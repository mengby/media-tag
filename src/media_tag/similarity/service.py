"""Current-ACL wrapper around explicit candidate pools; no cross-account library scan."""
from pathlib import Path

from media_tag.catalog import AccessDenied, CatalogError, RevisionConflict
from media_tag.files.service import FileError, FileService

from .contracts import AUDIO_VERSION, CLIP_VERSION, VISUAL_VERSION, AudioFingerprint, ClipFingerprint, SourceVersion, VisualFingerprint
from .engines import SimilarityError, audio_bytes, ranking_report, video_bytes, visual_bytes


class AuthorizedSimilarity:
    def __init__(self, router, tools):
        self.router, self.catalog, self.files, self.tools = router, router.catalog, FileService(router), tools

    def authorize(self, source, *, principal, edit=False):
        with self.router.database.transaction() as connection:
            asset = self.catalog._authorize(connection, source.asset_id, principal, 'edit' if edit else 'read')
            size=connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?',(asset['blob_sha256'],)).fetchone()[0]
        # Gate the current actual version before disclosing stored descriptors or decisions.
        self.files.authorize(source.asset_id, asset['blob_sha256'], principal=principal, for_bytes=True)
        if asset['revision'] != source.asset_revision or asset['blob_sha256'] != source.content_sha256:
            raise RevisionConflict('similarity source version changed')
        return dict(asset) | {'size_bytes':size}

    def describe(self, source: SourceVersion, *, principal, media_kind, workdir: Path | None = None, stream_index=None):
        asset=self.authorize(source, principal=principal)
        if asset['size_bytes'] > 128*1024**2:
            raise SimilarityError('source_byte_budget')
        opened = self.files.open(source.asset_id, source.content_sha256, principal=principal)
        try:
            chunks,total=[],0
            while chunk := opened.read(1024**2):
                total+=len(chunk)
                if total > 128*1024**2:
                    raise SimilarityError('source_byte_budget')
                chunks.append(chunk)
            data=b''.join(chunks)
        finally:
            opened.close()
        if len(data) > 128*1024**2:
            raise SimilarityError('source_byte_budget')
        if media_kind in ('image','animation'):
            result = visual_bytes(data, source)
        elif media_kind == 'audio':
            if workdir is None:
                raise ValueError('fresh private audio work directory required')
            result = audio_bytes(data, source, workdir, self.tools, stream_index=stream_index)
        elif media_kind == 'video':
            if workdir is None:
                raise ValueError('fresh private video work directory required')
            result = video_bytes(data, source, workdir, self.tools)
        else:
            raise ValueError('explicit supported media kind required')
        self.authorize(source, principal=principal)
        return result

    def describe_clip(self, source: SourceVersion, *, principal):
        """Reuse already accepted current CLIP features; never start inference or publish vocabulary."""
        from media_tag.drafts.service import DraftIndex
        self.authorize(source,principal=principal)
        with self.router.database.transaction() as connection:
            asset=self.catalog._authorize(connection,source.asset_id,principal)
            draft=DraftIndex(self.router).sync(connection,asset,persist=False)
        features=draft['features']
        if not features or len({(f['model_revision'],f['preprocess_version'],f['task_id'],f['lease_epoch'],f['report_sha256']) for f in features}) != 1:
            raise SimilarityError('clip_result_unavailable')
        result=ClipFingerprint(source=source,model_revision=features[0]['model_revision'],
            preprocess_version=features[0]['preprocess_version'],
            producer_task_id=features[0]['task_id'],producer_lease_epoch=features[0]['lease_epoch'],
            producer_report_sha256=features[0]['report_sha256'],
            frame_sha256=tuple(f['frame_sha256'] for f in features),
            vectors=tuple(tuple(float(x) for x in f['embedding']) for f in features))
        self.authorize(source,principal=principal)
        return result

    def validate_descriptor(self, descriptor, *, principal):
        self.authorize(descriptor.source,principal=principal)
        if (isinstance(descriptor,VisualFingerprint) and descriptor.algorithm_version != VISUAL_VERSION
                or isinstance(descriptor,AudioFingerprint) and descriptor.algorithm_version != AUDIO_VERSION):
            raise RevisionConflict('similarity descriptor profile changed')
        if isinstance(descriptor,ClipFingerprint):
            try:
                current=self.describe_clip(descriptor.source,principal=principal)
            except SimilarityError:
                raise RevisionConflict('current CLIP producer unavailable') from None
            if current.digest != descriptor.digest:
                raise RevisionConflict('current CLIP producer changed')

    def validate_candidate(self, candidate, *, principal):
        self.authorize(candidate.left,principal=principal)
        self.authorize(candidate.right,principal=principal)
        if candidate.algorithm_version in ('ct-visual-dhash-alpha-v1','ct-audio-logbands-v1'):
            raise RevisionConflict('similarity candidate profile changed')
        if candidate.algorithm_version==CLIP_VERSION:
            try:
                left=self.describe_clip(candidate.left,principal=principal)
                right=self.describe_clip(candidate.right,principal=principal)
            except SimilarityError:
                raise RevisionConflict('current CLIP producer unavailable') from None
            if left.digest != candidate.left_descriptor_sha256 or right.digest != candidate.right_descriptor_sha256:
                raise RevisionConflict('current CLIP producer changed')

    def candidates(self, query, pool, *, principal, limit=20):
        return self.candidate_report(query,pool,principal=principal,limit=limit)['items']

    def candidate_report(self, query, pool, *, principal, limit=20):
        self.validate_descriptor(query, principal=principal)
        if len(pool) > 1000:
            raise SimilarityError('candidate_pool_budget')
        permitted,stale = [],[]
        for descriptor in pool:
            try:
                self.validate_descriptor(descriptor, principal=principal)
            except RevisionConflict:
                stale.append({'source':descriptor.source.model_dump(mode='json'),'reason':'source_or_producer_changed'})
                continue
            except (CatalogError,FileError):
                continue  # Do not disclose inaccessible identities, scores or hidden pool counts.
            permitted.append(descriptor)
        report = ranking_report(query, permitted, limit=limit)
        report['skipped'].extend(stale)
        if stale:
            report['state']='partial' if report['items'] else 'insufficient_information'
        self.validate_descriptor(query, principal=principal)
        for result in report['items']:
            self.validate_candidate(result, principal=principal)
        return report
