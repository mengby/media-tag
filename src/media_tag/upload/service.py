"""Private, restart-safe offset uploads and a transactional registration/outbox boundary."""

from hashlib import sha256
import json
import os
import sqlite3
from pathlib import Path
import time
from uuid import uuid4

from media_tag.catalog import AccessDenied, CatalogError, NotFound, Principal, RevisionConflict
from media_tag.catalog.service import encode, _human
from media_tag.contracts.models import Versions, TaskLimits
from media_tag.router.models import JobSpec, StepSpec
from media_tag.storage import IntegrityError, StorageError, UnsafePath
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.evidence import _directory
from media_tag.workers.probe import MediaError
from .media import EXTENSIONS, validate_media
from .models import UploadOptions, UploadSpec


class UploadService:
    def __init__(self, router, tools, *, options=None, clock=time.time):
        self.router, self.catalog, self.database = router, router.catalog, router.database
        self.tools, self.options, self.clock = tools, options or UploadOptions(), clock
        self.files = FilesystemBackend(router.workspace)
        if isinstance(self.catalog.object_store, FilesystemBackend):
            stage, objects = router.workspace / 'jobs', self.catalog.object_store.root
            if stage.is_relative_to(objects) or objects.is_relative_to(stage):
                raise UnsafePath('upload staging and object roots must not overlap')

    def _row(self, connection, upload_id, principal):
        _human(principal)
        row = connection.execute('SELECT * FROM uploads WHERE upload_id=?', (upload_id,)).fetchone()
        if row is None:
            raise NotFound('upload is unavailable')
        if row['owner_id'] != principal.actor_id and principal.role != 'admin':
            raise AccessDenied('upload permission denied')
        return row

    def _view(self, connection, row, principal):
        if row['asset_id']:
            self.catalog._authorize(connection, row['asset_id'], principal)
        event = connection.execute('SELECT job_id,error_code FROM upload_outbox WHERE upload_id=?', (row['upload_id'],)).fetchone()
        validation = json.loads(row['validation_json']) if row['validation_json'] else {}
        spec = UploadSpec.model_validate_json(row['spec_json'])
        return dict(upload_id=row['upload_id'], state=row['state'], offset=row['offset'],
                    size_bytes=spec.size_bytes, sha256=spec.sha256, purpose=spec.purpose, filename=spec.filename,
                    chunk_bytes=self.options.chunk_bytes, asset_id=row['asset_id'], error_code=row['error_code'],
                    job_id=event['job_id'] if event else None, dispatch_error=event['error_code'] if event else None,
                    processing_history=validation.get('processing_history'))

    def create(self, spec: UploadSpec, *, principal):
        _human(principal)
        if spec.size_bytes > self.options.max_file_bytes or Path(spec.filename).suffix.lower() not in EXTENSIONS:
            raise ValueError('file exceeds intake policy or has an unsupported filename type')
        fingerprint = sha256(spec.model_dump_json().encode()).hexdigest()
        with self.database.transaction(write=True) as connection:
            old = connection.execute('SELECT * FROM uploads WHERE owner_id=? AND request_id=?', (principal.actor_id, spec.request_id)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise RevisionConflict('upload request ID was reused with different input')
                return self._view(connection, old, principal)
            if spec.known_asset_id:
                row = self.catalog._authorize(connection, spec.known_asset_id, Principal(actor_id=principal.actor_id))
                if row['blob_sha256'] != spec.sha256:
                    raise RevisionConflict('known asset does not match the declared content')
            count = connection.execute("SELECT count(*) FROM uploads WHERE owner_id=? AND state IN ('receiving','validating')", (principal.actor_id,)).fetchone()[0]
            reserved = sum(UploadSpec.model_validate_json(row[0]).size_bytes for row in connection.execute('SELECT spec_json FROM uploads WHERE owner_id=?', (principal.actor_id,)))
            if count >= self.options.max_open_per_owner or reserved + spec.size_bytes > self.options.max_reserved_per_owner:
                raise RevisionConflict('upload quota exceeded')
            upload_id = f'upload-{uuid4().hex}'
            connection.execute("INSERT INTO uploads(upload_id,owner_id,request_id,fingerprint,spec_json,state,created_at) VALUES(?,?,?,?,?,'receiving',?)",
                               (upload_id, principal.actor_id, spec.request_id, fingerprint, spec.model_dump_json(), self.clock()))
            return self._view(connection, self._row(connection, upload_id, principal), principal)

    def get(self, upload_id, *, principal):
        with self.database.transaction() as connection:
            return self._view(connection, self._row(connection, upload_id, principal), principal)

    def check_writable(self, upload_id, *, principal):
        # Called before reading a binary request body, then rechecked in append's write transaction.
        with self.database.transaction() as connection:
            return self._row(connection, upload_id, principal)['state'] == 'receiving'

    def append(self, upload_id, offset, data: bytes, digest, *, principal):
        if type(offset) is not int or offset < 0 or not data or len(data) > self.options.chunk_bytes:
            raise ValueError('invalid chunk offset or byte count')
        if sha256(data).hexdigest() != digest:
            raise IntegrityError('chunk hash mismatch')
        with self.database.transaction(write=True) as connection:
            row = self._row(connection, upload_id, principal)
            if row['state'] != 'receiving':
                raise RevisionConflict('upload is not receiving data')
            spec = UploadSpec.model_validate_json(row['spec_json'])
            previous = connection.execute('SELECT * FROM upload_chunks WHERE upload_id=? AND offset=?', (upload_id, offset)).fetchone()
            if previous:
                if previous['sha256'] != digest or previous['size'] != len(data):
                    raise RevisionConflict('chunk offset already contains different bytes')
                with self.files.open_verified(f'jobs/{upload_id}/parts/{digest}', digest, len(data)):
                    pass
                return self._view(connection, row, principal)
            if offset != row['offset'] or offset + len(data) > spec.size_bytes:
                raise RevisionConflict('chunk does not match the committed upload offset')
            folder = _directory(self.router.workspace, f'jobs/{upload_id}/parts')
            # Exclusive publication, including crash-orphan reuse; never overwrite a source/old part.
            temporary = folder / f'.part-{uuid4().hex}'
            try:
                with temporary.open('xb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                try:
                    os.link(temporary, folder / digest)
                except FileExistsError:
                    pass
                with self.files.open_verified(f'jobs/{upload_id}/parts/{digest}', digest, len(data)):
                    pass
            finally:
                temporary.unlink(missing_ok=True)
            connection.execute('INSERT INTO upload_chunks VALUES(?,?,?,?)', (upload_id, offset, len(data), digest))
            connection.execute('UPDATE uploads SET offset=offset+? WHERE upload_id=?', (len(data), upload_id))
            return self._view(connection, self._row(connection, upload_id, principal), principal)

    def cancel(self, upload_id, *, principal):
        with self.database.transaction(write=True) as connection:
            row = self._row(connection, upload_id, principal)
            if row['state'] == 'completed':
                raise RevisionConflict('completed upload cannot be cancelled')
            connection.execute("UPDATE uploads SET state='cancelled',epoch=epoch+1,error_code='cancel_requested' WHERE upload_id=?", (upload_id,))

    def _known(self, connection, spec, owner):
        if spec.known_asset_id:
            row = self.catalog._authorize(connection, spec.known_asset_id, Principal(actor_id=owner))
            if row['blob_sha256'] != spec.sha256:
                raise RevisionConflict('known asset content changed')
            return row
        visible = []
        for candidate in connection.execute('SELECT a.* FROM assets a JOIN variants v USING(asset_id) WHERE blob_sha256=? AND deleted_at IS NULL', (spec.sha256,)):
            try:
                self.catalog._authorize(connection, candidate['asset_id'], Principal(actor_id=owner))
                visible.append(candidate)
            except AccessDenied:
                pass
        if len(visible) > 1:
            raise RevisionConflict('multiple visible variants match; specify known_asset_id')
        return visible[0] if visible else None

    def finalize(self, upload_id, *, principal):
        with self.database.transaction(write=True) as connection:
            row = self._row(connection, upload_id, principal)
            if row['state'] == 'completed':
                return self._view(connection, row, principal)
            spec = UploadSpec.model_validate_json(row['spec_json'])
            if row['state'] not in ('receiving', 'validating') or row['offset'] != spec.size_bytes:
                raise RevisionConflict('upload is incomplete or closed')
            if row['state'] == 'validating' and row['lease_until'] > self.clock():
                raise RevisionConflict('another finalizer owns the upload')
            epoch, owner = row['epoch'] + 1, row['owner_id']
            connection.execute("UPDATE uploads SET state='validating',epoch=?,lease_until=?,error_code=NULL WHERE upload_id=?", (epoch, self.clock() + self.options.finalize_seconds, upload_id))
            chunks = connection.execute('SELECT * FROM upload_chunks WHERE upload_id=? ORDER BY offset', (upload_id,)).fetchall()
        try:
            folder = _directory(self.router.workspace, f'jobs/{upload_id}')
            name = f'input-{epoch}.bin'
            digest, size = sha256(), 0
            with (folder / name).open('xb') as output:
                for chunk in chunks:
                    if chunk['offset'] != size:
                        raise IntegrityError('chunk history has a gap')
                    with self.files.open_verified(f"jobs/{upload_id}/parts/{chunk['sha256']}", chunk['sha256'], chunk['size']) as stream:
                        while data := stream.read(1024 * 1024):
                            digest.update(data); size += len(data); output.write(data)
                output.flush(); os.fsync(output.fileno())
            if (size, digest.hexdigest()) != (spec.size_bytes, spec.sha256):
                raise IntegrityError('complete upload hash or size mismatch')
            source = FilesystemBackend(folder)
            with source.open_verified(name, spec.sha256, spec.size_bytes) as stream:
                validation = validate_media(Path(stream.name), self.tools, filename=spec.filename)
                stream.seek(0)
                blob = self.catalog.object_store.ingest(stream)
            if (blob.sha256, blob.size_bytes) != (spec.sha256, spec.size_bytes):
                raise IntegrityError('validated upload changed during ingest')
            with self.database.transaction(write=True) as connection:
                current = self._row(connection, upload_id, principal)
                if current['state'] != 'validating' or current['epoch'] != epoch or current['lease_until'] <= self.clock():
                    raise RevisionConflict('upload finalizer was cancelled or fenced')
                known = self._known(connection, spec, owner)
                validation['processing_history'] = 'registered_variant' if known and known['kind'] == 'derived' else 'known_asset' if known else 'external_unknown'
                if known:
                    asset_id = known['asset_id']  # Proven upload receipt keeps its own source specification.
                else:
                    asset_id = self.catalog._register_blob_tx(connection, blob, source_key=f'jobs/{upload_id}/{name}', source_backend_id='upload-workspace',
                        principal=Principal(actor_id=owner), request_id=upload_id, name=spec.filename, source=spec.source).asset_id
                connection.execute("UPDATE uploads SET state='completed',asset_id=?,validation_json=? WHERE upload_id=?", (asset_id, encode(validation), upload_id))
                # Reused derived outputs retain their privacy gate and never re-enter the original pipeline.
                if not known or known['kind'] == 'original':
                    connection.execute('INSERT INTO upload_outbox(event_id,upload_id,asset_id,owner_id,purpose) VALUES(?,?,?,?,?)',
                                       (f'event-{uuid4().hex}', upload_id, asset_id, owner, spec.purpose))
                return self._view(connection, self._row(connection, upload_id, principal), principal)
        except Exception as exc:
            code = exc.code if isinstance(exc, MediaError) else 'integrity_failed' if isinstance(exc, IntegrityError) else 'finalize_failed'
            retry = isinstance(exc, (OSError, StorageError, sqlite3.Error)) and not isinstance(exc, IntegrityError) or code in {'tool_unavailable', 'tool_missing', 'tool_timeout'}
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE uploads SET state=?,error_code=? WHERE upload_id=? AND epoch=? AND state='validating'",
                                   ('receiving' if retry else 'rejected', code, upload_id, epoch))
            raise

    def dispatch_pending(self, *, principal):
        _human(principal)
        with self.database.transaction() as connection:
            events = connection.execute('SELECT * FROM upload_outbox WHERE job_id IS NULL ORDER BY rowid').fetchall()
            fingerprint = connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()[0]
        for event in events:
            if event['owner_id'] != principal.actor_id and principal.role != 'admin':
                continue
            error, job_id = None, None
            try:
                versions = Versions(rules_version=f'sha256:{fingerprint}', preprocess_version='d02-cpu-v1')
                steps = (StepSpec(key='probe', stage='probe', versions=versions),)
                if event['purpose'] == 'library':
                    steps += (StepSpec(key='evidence', stage='evidence', depends_on=('probe',), versions=versions),)
                    for kind in ('ocr', 'face', 'clip'):
                        definition = self.router.stages.get(kind)
                        if definition and definition.required_model_revision:
                            model_versions = Versions(rules_version=versions.rules_version,
                                preprocess_version=definition.required_preprocess_version,
                                model_revision=definition.required_model_revision)
                            steps += (StepSpec(key=kind, stage=kind, depends_on=('evidence',), versions=model_versions,
                                               limits=TaskLimits(gpu_slots=definition.required_gpu_slots)),)
                job = self.router.submit(JobSpec(asset_id=event['asset_id'], steps=steps), request_id=event['event_id'], principal=Principal(actor_id=event['owner_id']))
                job_id = job['job_id']
            except (CatalogError, ValueError):
                error = 'dispatch_unavailable'
            with self.database.transaction(write=True) as connection:
                connection.execute('UPDATE upload_outbox SET job_id=?,error_code=? WHERE event_id=? AND job_id IS NULL', (job_id, error, event['event_id']))

    def complete(self, upload_id, *, principal):
        self.finalize(upload_id, principal=principal)
        self.dispatch_pending(principal=principal)
        return self.get(upload_id, principal=principal)
