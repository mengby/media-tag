"""Read-only file policy, manifests and preview selection from adopted D02 evidence."""

from hashlib import sha256
import json
import os

from PIL import Image

from media_tag.catalog import AccessDenied, NotFound, Principal
from media_tag.contracts.models import TaskEnvelope, WorkerResult, validate_result_for_task
from media_tag.storage import StorageError
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import finite_number
from media_tag.transforms.policy import review_state

SCHEMA = 'd17-files-v1'


class FileError(Exception):
    def __init__(self, status, code, *, retryable=False):
        self.status, self.code, self.retryable = status, code, retryable
        super().__init__(code)

    def payload(self):
        return {'schema_version': SCHEMA, 'error': {'code': self.code, 'retryable': self.retryable}}


def media_type(stream):
    position = stream.tell(); data = stream.read(4096); stream.seek(position)
    if data.startswith(b'\x89PNG\r\n\x1a\n'): return 'image/png'
    if data.startswith(b'\xff\xd8\xff'): return 'image/jpeg'
    if data[:6] in (b'GIF87a', b'GIF89a'): return 'image/gif'
    if data.startswith(b'RIFF'):
        return {b'WEBP': 'image/webp', b'WAVE': 'audio/wav', b'AVI ': 'video/x-msvideo'}.get(data[8:12], 'application/octet-stream')
    if data[4:8] == b'ftyp': return 'video/mp4'  # ISO BMFF; exact codecs remain in technical metadata.
    if data.startswith(b'\x1aE\xdf\xa3'): return 'video/x-matroska'
    if data.startswith(b'fLaC'): return 'audio/flac'
    if data.startswith(b'OggS'): return 'application/ogg'
    if data.startswith(b'ID3'): return 'audio/mpeg'
    return 'application/octet-stream'


class OpenedFile:
    def __init__(self, service, asset_id, source_sha, principal, manager, stream, digest, size, name,
                 mime, *, preview_id=None, preview_sha=None):
        self.service, self.asset_id, self.source_sha, self.principal = service, asset_id, source_sha, principal
        self.manager, self.stream, self.digest, self.size, self.name, self.mime = manager, stream, digest, size, name, mime
        self.preview_id, self.preview_sha = preview_id, preview_sha
        self.closed = False
        try:
            self.stamp = self._stamp()
        except (OSError, AttributeError):
            self.stamp = None

    def _stamp(self):
        stat = os.fstat(self.stream.fileno())
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def close(self):
        if not self.closed:
            self.closed = True
            self.manager.__exit__(None, None, None)

    def read(self, count):
        self.service.authorize(self.asset_id, self.source_sha, principal=self.principal, for_bytes=True)
        if self.preview_id:
            self.service.preview_record(self.asset_id, self.source_sha, self.preview_id, self.preview_sha, principal=self.principal)
        if self.stamp is not None and self.stamp != self._stamp():
            raise FileError(503, 'content_changed')
        data = self.stream.read(count)
        if self.stamp is not None and self.stamp != self._stamp():
            raise FileError(503, 'content_changed')
        return data


class FileService:
    def __init__(self, router, *, review_authorizer=None):
        self.router, self.catalog, self.database = router, router.catalog, router.database
        self.files = FilesystemBackend(router.workspace)
        self.review_authorizer = review_authorizer

    @staticmethod
    def _role(principal):
        if principal.role not in ('editor', 'admin', 'reader'):
            raise FileError(403, 'file_access_denied')

    def _asset(self, connection, asset_id, expected_sha, principal):
        self._role(principal)
        try:
            asset = self.catalog._authorize(connection, asset_id, principal)
        except AccessDenied:
            raise FileError(403, 'file_access_denied') from None
        except NotFound:
            raise FileError(404, 'asset_unavailable') from None
        if expected_sha is not None and asset['blob_sha256'] != expected_sha:
            raise FileError(412, 'source_hash_mismatch')
        return asset

    def _gate(self, connection, asset, *, review_asset_id=None):
        visited = set()
        current = asset
        while current['kind'] == 'derived':
            if current['asset_id'] in visited:
                return 'lineage_unavailable'
            visited.add(current['asset_id'])
            variant = connection.execute('SELECT * FROM variants WHERE asset_id=?', (current['asset_id'],)).fetchone()
            if variant is None or variant['output_sha256'] != current['blob_sha256']:
                return 'lineage_unavailable'
            privacy=review_state(connection,variant)
            if privacy not in ('not_applicable','approved') and current['asset_id'] != review_asset_id:
                return 'privacy_review_revoked' if privacy=='revoked' else 'privacy_review_pending'
            execution = connection.execute('''SELECT t.active,t.state,t.lease_epoch,j.cancelled,a.released_at
                FROM router_tasks t JOIN router_jobs j USING(job_id) JOIN router_attempts a USING(task_id)
                WHERE t.task_id=? AND a.lease_epoch=?''', (variant['task_id'], variant['lease_epoch'])).fetchone()
            if execution is None or not execution['active'] or execution['state'] != 'succeeded' or execution['cancelled'] or execution['lease_epoch'] != variant['lease_epoch'] or execution['released_at'] is None:
                return 'producer_unavailable'
            parent = connection.execute('SELECT * FROM assets WHERE asset_id=? AND deleted_at IS NULL', (current['parent_asset_id'],)).fetchone()
            if parent is None or parent['blob_sha256'] != variant['source_sha256']:
                return 'lineage_unavailable'
            current = parent
        return None

    def authorize(self, asset_id, expected_sha, *, principal, for_bytes=False):
        with self.database.transaction() as connection:
            asset = self._asset(connection, asset_id, expected_sha, principal)
            review_asset_id=None
            if self.review_authorizer:
                self.review_authorizer(connection, asset, principal)
                review_asset_id=asset_id
            blocked = self._gate(connection, asset, review_asset_id=review_asset_id)
            if for_bytes and blocked:
                raise FileError(409, blocked)
            return dict(asset), blocked

    @staticmethod
    def _entry(asset_id, digest, operation):
        return f'/v1/assets/{asset_id}/{operation}?sha256={digest}'

    def open(self, asset_id, expected_sha, *, principal):
        self.authorize(asset_id, expected_sha, principal=principal, for_bytes=True)
        with self.database.transaction() as connection:
            asset = self._asset(connection, asset_id, expected_sha, principal)
            size = connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?', (expected_sha,)).fetchone()[0]
            locations = connection.execute('SELECT backend_id,object_key FROM storage_locations WHERE blob_sha256=? ORDER BY CASE WHEN backend_id=? THEN 0 ELSE 1 END,backend_id', (expected_sha, self.catalog.backend_id)).fetchall()
            location = next((row for row in locations if row['backend_id'] in self.catalog.backends), None)
            if location is None:
                raise FileError(503, 'backend_unavailable', retryable=True)
        try:
            manager = self.catalog.backends[location['backend_id']].open_verified(location['object_key'], expected_sha, size)
            stream = manager.__enter__()
        except (StorageError, OSError):
            raise FileError(503, 'content_unavailable', retryable=True) from None
        try:
            mime = media_type(stream)
            self.authorize(asset_id, expected_sha, principal=principal, for_bytes=True)
            return OpenedFile(self, asset_id, expected_sha, principal, manager, stream, expected_sha, size, asset['name'], mime)
        except (OSError, ValueError, StorageError):
            manager.__exit__(None, None, None)
            raise FileError(503, 'backend_stream_unavailable', retryable=True) from None
        except BaseException:
            manager.__exit__(None, None, None)
            raise

    def _attempts(self, connection, asset_id):
        return connection.execute('''SELECT a.envelope_json,a.result_json,a.lease_epoch,t.spec_json,j.job_id
            FROM router_tasks t JOIN router_jobs j USING(job_id) JOIN router_attempts a USING(task_id)
            WHERE j.asset_id=? AND j.cancelled=0 AND t.active=1 AND t.state='succeeded'
            AND t.lease_epoch=a.lease_epoch AND a.released_at IS NOT NULL AND a.result_json IS NOT NULL
            ORDER BY a.started_at DESC,t.task_id LIMIT 32''', (asset_id,)).fetchall()

    def _technical(self, connection, asset):
        variant = connection.execute('SELECT validation_json,time_map_json,privacy_status FROM variants WHERE asset_id=?', (asset['asset_id'],)).fetchone()
        if variant:
            data = json.loads(variant['validation_json'])
        else:
            upload = connection.execute("SELECT validation_json FROM uploads WHERE asset_id=? AND state='completed' ORDER BY created_at DESC LIMIT 1", (asset['asset_id'],)).fetchone()
            data = json.loads(upload[0]) if upload else {}
        report_state = 'ready' if data else 'not_available'
        if not data:
            for row in self._attempts(connection, asset['asset_id']):
                task, result = TaskEnvelope.model_validate_json(row['envelope_json']), WorkerResult.model_validate_json(row['result_json'])
                if task.stage != 'probe' or task.input_refs[0].sha256 != asset['blob_sha256'] or result.status != 'succeeded':
                    continue
                validate_result_for_task(task, result)
                report = next((ref for ref in result.artifacts if ref.kind == 'technical_report' and ref.ref.startswith(task.output_prefix)), None)
                if report:
                    try:
                        with self.files.open_read(report.ref) as stream:
                            raw = stream.read(2 * 1024**2 + 1)
                        if len(raw) > 2 * 1024**2 or sha256(raw).hexdigest() != report.sha256:
                            raise FileError(503, 'technical_report_changed')
                        data = json.loads(raw)
                        report_state = 'ready'
                    except (StorageError, OSError, ValueError, FileError):
                        data, report_state = {}, 'unavailable'
                    break
        keys = ('kind', 'width', 'height', 'display_width', 'display_height', 'duration_seconds', 'start_seconds', 'has_audio')
        technical = {key: data.get(key) for key in keys}
        technical['report_state'] = report_state
        streams = [s for s in data.get('raw_metadata', {}).get('streams', []) if s.get('codec_type') in ('video', 'audio') and not s.get('disposition', {}).get('attached_pic')]
        starts, durations = [finite_number(s.get('start_time')) for s in streams], [finite_number(s.get('duration')) for s in streams]
        known = bool(streams) and all(value is not None for value in (*starts, *durations))
        technical['time_basis'] = {'normalization_origin_seconds': min(starts) if known else None,
                                  'duration_seconds': max(s + d for s, d in zip(starts, durations)) - min(starts) if known else None,
                                  'kind': 'current_file_normalized_seconds'}
        return technical

    @staticmethod
    def _transform(connection, asset_id):
        row = connection.execute('SELECT p.spec_json,v.recipe_hash,v.time_map_json FROM variants v JOIN variant_plans p USING(plan_id) WHERE v.asset_id=?', (asset_id,)).fetchone()
        if row is None:
            return None
        data = json.loads(row['spec_json'])
        return {'recipe_hash': row['recipe_hash'], 'recipe': data['spec']['recipe'], 'cumulative_speed': data['cumulative_speed'],
                'parent_to_output_time_map': json.loads(row['time_map_json']), 'apply_transform_again': False}

    def details(self, asset_id, *, principal):
        with self.database.transaction() as connection:
            asset = self._asset(connection, asset_id, None, principal)
            snapshot = self.catalog._snapshot(connection, asset)
            blocked = self._gate(connection, asset)
            versions = []
            candidates = connection.execute('''SELECT * FROM assets WHERE root_asset_id=? AND deleted_at IS NULL AND
                (? OR owner_id=? OR asset_id IN (SELECT asset_id FROM asset_permissions WHERE subject_id=? AND permission IN ('read','edit')))
                ORDER BY created_at,asset_id LIMIT 51''', (asset['root_asset_id'], int(principal.role == 'admin'), principal.actor_id, principal.actor_id)).fetchall()
            for candidate in candidates[:50]:
                try:
                    self._asset(connection, candidate['asset_id'], None, principal)
                except FileError:
                    continue
                versions.append({'asset_id': candidate['asset_id'], 'sha256': candidate['blob_sha256'], 'kind': candidate['kind'], 'file_access': self._gate(connection, candidate) or 'authorized'})
            sources = [{'source_id': s.source_id, 'label': s.label, 'reference': s.reference, 'attribution': s.attribution, 'license_status': s.license_status} for s in snapshot.sources]
            return dict(schema_version=SCHEMA, asset_id=asset_id, root_asset_id=asset['root_asset_id'], parent_asset_id=asset['parent_asset_id'],
                        sha256=asset['blob_sha256'], size_bytes=snapshot.size_bytes, revision=asset['revision'], name=asset['name'], kind=asset['kind'],
                        index_status=asset['index_status'], effective_values=[v.model_dump(mode='json') for v in snapshot.effective_values],
                        suggestions=[s.model_dump(mode='json') for s in snapshot.suggestions], sources=sources, versions=versions,
                        versions_truncated=len(candidates) > 50,
                        technical=self._technical(connection, asset), file_access=blocked or 'authorized',
                        transform=self._transform(connection, asset_id),
                        current_use_review='not_checked', license_status='unverified',
                        download_manifest=self._entry(asset_id, asset['blob_sha256'], 'download-manifest'))

    def download_manifest(self, asset_id, expected_sha, *, principal):
        opened = self.open(asset_id, expected_sha, principal=principal)
        try:
            asset, _ = self.authorize(asset_id, expected_sha, principal=principal, for_bytes=True)
            with self.database.transaction() as connection:
                technical = self._technical(connection, asset)
                transform = self._transform(connection, asset_id)
            return dict(schema_version=SCHEMA, asset_id=asset_id, sha256=expected_sha, size_bytes=opened.size,
                        revision=asset['revision'], media_type=opened.mime, etag=f'"{expected_sha}"',
                        content=self._entry(asset_id, expected_sha, 'content'), range_unit='bytes',
                        whole_file=True, technical=technical, transform=transform, media_type_verification='signature',
                        current_use_review='not_checked', license_status='unverified')
        finally:
            opened.close()

    def _previews(self, connection, asset_id, expected_sha):
        rows = self._attempts(connection, asset_id)
        for row in rows:
            task = TaskEnvelope.model_validate_json(row['envelope_json'])
            if task.stage != 'evidence' or task.input_refs[0].sha256 != expected_sha:
                continue
            result = WorkerResult.model_validate_json(row['result_json'])
            validate_result_for_task(task, result)
            if result.status == 'not_applicable':
                return [], 'not_applicable'
            if result.status != 'succeeded':
                continue
            if len(result.evidence) > 12:
                raise FileError(503, 'preview_budget_exceeded')
            records = []
            for item in result.evidence:
                if item.kind not in ('image', 'frame') or item.source_sha256 != expected_sha or not item.artifact.ref.startswith(task.output_prefix):
                    continue
                preview_id = 'preview-' + sha256(f'{asset_id}:{expected_sha}:{task.task_id}:{task.lease_epoch}:{item.evidence_id}:{item.artifact.sha256}'.encode()).hexdigest()[:32]
                records.append(dict(preview_id=preview_id, artifact=item.artifact, source_seconds=item.start_seconds, declared_preprocess_version=task.versions.preprocess_version))
            if records:
                return records, 'ready'
        for row in connection.execute('''SELECT t.state,t.spec_json,j.cancelled FROM router_tasks t JOIN router_jobs j USING(job_id)
            WHERE j.asset_id=? AND t.active=1 ORDER BY j.created_at DESC,t.generation DESC LIMIT 32''', (asset_id,)):
            if json.loads(row['spec_json'])['stage'] != 'evidence':
                continue
            if row['cancelled'] or row['state'] == 'cancelled':
                return [], 'cancelled'
            if row['state'] in ('failed', 'needs_attention'):
                return [], 'failed'
            return [], 'pending'
        return [], 'not_requested'

    def preview_record(self, asset_id, expected_sha, preview_id, preview_sha, *, principal):
        self.authorize(asset_id, expected_sha, principal=principal, for_bytes=True)
        with self.database.transaction() as connection:
            records, _ = self._previews(connection, asset_id, expected_sha)
            record = next((item for item in records if item['preview_id'] == preview_id), None)
            if record is None:
                raise FileError(404, 'preview_unavailable')
            if record['artifact'].sha256 != preview_sha:
                raise FileError(412, 'preview_hash_mismatch')
            return record

    def open_preview(self, asset_id, expected_sha, preview_id, preview_sha, *, principal):
        record = self.preview_record(asset_id, expected_sha, preview_id, preview_sha, principal=principal)
        artifact = record['artifact']
        manager = self.files.open_read(artifact.ref)
        try:
            stream = manager.__enter__()
        except (StorageError, OSError):
            raise FileError(503, 'preview_content_unavailable', retryable=True) from None
        try:
            digest, size = sha256(), 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                if size > 8 * 1024**2:
                    raise FileError(503, 'preview_budget_exceeded')
                digest.update(chunk)
            if digest.hexdigest() != artifact.sha256:
                raise FileError(503, 'preview_content_changed')
            stream.seek(0)
            with Image.open(stream) as image:
                if image.format != 'PNG' or image.width * image.height > 2048**2:
                    raise FileError(503, 'preview_format_invalid')
                image.load()
                dimensions = image.size
            # PIL can close the passed handle when exiting; retain our verified handle and rewind it.
            stream.seek(0)
            self.preview_record(asset_id, expected_sha, preview_id, preview_sha, principal=principal)
            opened = OpenedFile(self, asset_id, expected_sha, principal, manager, stream, artifact.sha256, size,
                                f'{preview_id}.png', 'image/png', preview_id=preview_id, preview_sha=preview_sha)
            opened.dimensions = dimensions
            return opened
        except BaseException as exc:
            manager.__exit__(None, None, None)
            if isinstance(exc, (OSError, ValueError)):
                raise FileError(503, 'preview_format_invalid') from None
            raise

    def preview_manifest(self, asset_id, expected_sha, *, principal):
        opened = self.open(asset_id, expected_sha, principal=principal)
        try:
            with self.database.transaction() as connection:
                records, state = self._previews(connection, asset_id, expected_sha)
                asset = self._asset(connection, asset_id, expected_sha, principal)
                technical = self._technical(connection, asset)
            resources = []
            for record in records:
                preview = self.open_preview(asset_id, expected_sha, record['preview_id'], record['artifact'].sha256, principal=principal)
                try:
                    resources.append(dict(kind='thumbnail', preview_id=record['preview_id'], sha256=preview.digest, size_bytes=preview.size,
                        media_type='image/png', width=preview.dimensions[0], height=preview.dimensions[1], source_seconds=record['source_seconds'],
                        content=f'/v1/assets/{asset_id}/previews/{record["preview_id"]}/content?sha256={expected_sha}&preview_sha256={preview.digest}',
                        transform={'format': 'PNG', 'declared_preprocess_version': record['declared_preprocess_version'], 'continuous_observation': False}))
                finally:
                    preview.close()
            inline = opened.mime.startswith(('video/', 'audio/', 'image/')) or opened.mime == 'application/ogg'
            if inline:
                resources.insert(0, dict(kind='source_media', sha256=expected_sha, size_bytes=opened.size, media_type=opened.mime,
                    content=self._entry(asset_id, expected_sha, 'preview-content'), time_map={'kind': 'identity', 'version_sha256': expected_sha},
                    time_basis=technical['time_basis'], compatibility='client_dependent', whole_file=True))
            return dict(schema_version=SCHEMA, asset_id=asset_id, source_sha256=expected_sha, resources=resources,
                        state='ready' if inline or resources else state, thumbnail_state=state,
                        reason=None if resources else 'no_visual_preview' if state == 'not_applicable' else f'preview_{state}',
                        status_query=self._entry(asset_id, expected_sha, 'preview-manifest'), full_video_observed=False)
        finally:
            opened.close()
