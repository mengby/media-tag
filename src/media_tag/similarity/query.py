"""Authorized candidate retrieval from current server-produced descriptors only."""
from typing import Annotated

from pydantic import Field

from media_tag.catalog import AccessDenied, CatalogError, RevisionConflict
from media_tag.contracts.models import ContractModel, Identifier
from media_tag.files.service import FileError, FileService
from media_tag.models.contracts import ModelFailure
from media_tag.storage import StorageError

from .pipeline import verified
from .service import AuthorizedSimilarity

PASS_SOURCE_BYTES = 256*1024**2
# A request hashes the same bounded source set twice, at most 512 MiB total.
# Individual JSON reports are separately limited to 4 MiB by read_json.


class CandidateQuery(ContractModel):
    asset_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=32)]
    limit: Annotated[int, Field(ge=1, le=32)] = 20


class DescriptorQuery:
    def __init__(self, router):
        self.router, self.files = router, FileService(router)

    def _snapshot(self, asset_id, principal):
        if principal.role not in ('reader', 'editor', 'admin'):
            raise AccessDenied('human read access required')
        with self.router.database.transaction() as connection:
            asset = dict(self.router.catalog._authorize(connection, asset_id, principal))
            size = connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?', (asset['blob_sha256'],)).fetchone()[0]
            row = connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at,j.cancelled,j.paused
                FROM router_tasks t JOIN router_jobs j ON j.job_id=t.job_id
                LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                WHERE j.asset_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')='similarity'
                ORDER BY t.rowid DESC LIMIT 1''', (asset_id,)).fetchone()
        self.files.authorize(asset_id, asset['blob_sha256'], principal=principal, for_bytes=True)
        return asset, size, dict(row) if row else None

    def load(self, asset_id, *, principal, remaining=PASS_SOURCE_BYTES):
        asset, size, row = self._snapshot(asset_id, principal)
        if size > remaining:
            return None, 'request_byte_budget', 0
        if row is None:
            return None, 'not_analyzed', 0
        if row['cancelled'] or row['paused']:
            return None, 'inactive_job', 0
        if row['state'] != 'succeeded' or row['released_at'] is None:
            code = row['error_code'] or ''
            if code in ('audio_track_selection_required', 'visual_track_selection_required'):
                return None, 'awaiting_track_selection', 0
            if code.endswith('_budget'):
                return None, 'awaiting_budget', 0
            return None, row['state'], 0
        try:
            descriptor = verified(self.router, row, asset)
        except (ValueError, OSError, StorageError, ModelFailure, KeyError, TypeError):
            return None, 'unavailable', size
        if descriptor.source.asset_revision != asset['revision']:
            return None, 'source_revision_changed', size
        # No SQLite writer is held while hashing reports or whole input copies.
        current, _, latest = self._snapshot(asset_id, principal)
        if (current['revision'] != asset['revision'] or current['blob_sha256'] != asset['blob_sha256']
                or latest is None or any(latest[key] != row[key] for key in
                    ('task_id', 'lease_epoch', 'state', 'released_at', 'result_json', 'cancelled', 'paused'))):
            raise RevisionConflict('similarity producer changed during verification')
        return descriptor, 'ready', size

    def status(self, asset_id, *, principal):
        descriptor, state, _ = self.load(asset_id, principal=principal)
        return {'asset_id': asset_id, 'state': state, 'algorithm_version': descriptor.algorithm_version if descriptor else None,
            'duplicate_confirmed': False, 'labels_copied': False}

    def candidates(self, asset_id, spec, *, principal):
        query, state, used = self.load(asset_id, principal=principal)
        if query is None:
            return {'state': state, 'items': [], 'skipped': [], 'duplicate_confirmed': False, 'labels_copied': False}
        pool, skipped = [], []
        for target_id in dict.fromkeys(spec.asset_ids):
            if target_id == asset_id:
                continue
            try:
                descriptor, state, consumed = self.load(target_id, principal=principal, remaining=PASS_SOURCE_BYTES-used)
            except (CatalogError, FileError):
                continue  # No hidden identity, reason, score or count is disclosed.
            used += consumed
            if descriptor is None:
                skipped.append({'asset_id': target_id, 'reason': state})
            else:
                pool.append(descriptor)
        # Candidate cores consume only the descriptors above, never caller vectors.
        report = AuthorizedSimilarity(self.router, None).candidate_report(query, pool, principal=principal, limit=spec.limit)
        report['skipped'].extend(skipped)
        if skipped:
            report['state'] = 'partial' if report['items'] else 'insufficient_information'
        # Recheck that each returned pair still uses the current producer, including
        # a newer queued/failed attempt. Never fall back to an old successful task.
        for descriptor in (query, *pool):
            current, _, _ = self.load(descriptor.source.asset_id, principal=principal)
            if current is None or current.digest != descriptor.digest:
                raise RevisionConflict('similarity producer changed during ranking')
        return report
