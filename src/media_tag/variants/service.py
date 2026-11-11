"""Private transform plans and trusted, fenced result publication; never privacy approval."""

from contextlib import contextmanager
from fractions import Fraction
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from media_tag.catalog import AccessDenied, NotFound, Principal, RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import _human, _identifier, encode
from media_tag.contracts.models import TaskEnvelope, WorkerResult
from media_tag.storage import IntegrityError
from media_tag.upload.media import validate_media
from media_tag.workers.probe import finite_number
from .models import FixedTimeMap, PlanSpec, SourceClock
from media_tag.transforms.policy import reviewed,review_state,has_privacy_lineage


def _stream_clock(probe, selected_audio):
    streams = [stream for stream in probe['raw_metadata']['streams'] if stream['index'] == probe['video_stream_index'] or stream['index'] == selected_audio]
    starts = {str(stream['index']): finite_number(stream.get('start_time')) for stream in streams}
    durations = {str(stream['index']): finite_number(stream.get('duration')) for stream in streams}
    if not starts or any(value is None for value in starts.values()) or any(value is None or value <= 0 for value in durations.values()):
        raise ValueError('selected stream timing is unknown; do not assume a zero origin or container duration')
    origin = min(starts.values())
    end = max(starts[key] + durations[key] for key in starts)
    return origin, end - origin, {key: value - origin for key, value in starts.items()}, durations


class VariantService:
    def __init__(self, router, tools, *, output_stages=()):
        self.router, self.catalog, self.database, self.tools = router, router.catalog, router.database, tools
        # Empty in normal bootstrap until D10/D11 supply a real registered validation adapter.
        self.output_stages = frozenset(output_stages)

    @contextmanager
    def _content(self, asset_id, principal):
        with self.database.transaction() as connection:
            row = self.catalog._authorize(connection, asset_id, principal)
            if row['kind'] == 'derived':
                variant = connection.execute('SELECT * FROM variants WHERE asset_id=?', (asset_id,)).fetchone()
                if variant is None or variant['privacy_status'] == 'pending' and not reviewed(connection,variant) and row['owner_id'] != principal.actor_id and principal.role != 'admin':
                    raise AccessDenied('unreviewed output is private to its owner')
                if row['owner_id']!=principal.actor_id and principal.role!='admin':
                    from media_tag.files.service import FileService
                    if FileService(self.router)._gate(connection,row):
                        raise AccessDenied('processing cannot bypass a blocked privacy lineage')
            size = connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?', (row['blob_sha256'],)).fetchone()[0]
            location = connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=? AND backend_id=?', (row['blob_sha256'], self.catalog.backend_id)).fetchone()
            if location is None:
                raise NotFound('local processing content unavailable')
        with self.catalog.object_store.open_verified(location[0], row['blob_sha256'], size) as stream:
            yield stream

    def _plan(self, connection, plan_id, principal):
        _human(principal)
        plan = connection.execute('SELECT * FROM variant_plans WHERE plan_id=?', (plan_id,)).fetchone()
        if plan is None:
            raise NotFound('variant plan is unavailable')
        if plan['owner_id'] != principal.actor_id and principal.role != 'admin':
            raise AccessDenied('variant plan permission denied')
        self.catalog._authorize(connection, plan['parent_asset_id'], Principal(actor_id=plan['owner_id']))
        return plan

    @staticmethod
    def _view(plan):
        return dict(plan_id=plan['plan_id'], parent_asset_id=plan['parent_asset_id'], source_sha256=plan['source_sha256'],
                    recipe_hash=plan['recipe_hash'], contract=json.loads(plan['spec_json']))

    def get_plan(self, plan_id, *, principal):
        with self.database.transaction() as connection:
            return self._view(self._plan(connection, plan_id, principal))

    def create_plan(self, spec: PlanSpec, *, request_id, principal):
        _human(principal); _identifier(request_id)
        delegate = Principal(actor_id=principal.actor_id)
        with self.database.transaction() as connection:
            parent = self.catalog._authorize(connection, spec.parent_asset_id, delegate)
            self.catalog._expected(parent, spec.expected_revision)
            previous = connection.execute('SELECT p.spec_json FROM variants v JOIN variant_plans p USING(plan_id) WHERE v.asset_id=?', (spec.parent_asset_id,)).fetchone()
            if parent['kind'] == 'derived' and (not spec.chain_from_derived or previous is None):
                raise ValueError('processing a derived input requires explicit lineage chaining')
            if has_privacy_lineage(connection,parent):
                spec=spec.model_copy(update={'recipe':spec.recipe.model_copy(update={'privacy_review_required':True})})
            cumulative = json.loads(previous[0])['cumulative_speed'] if previous else 1.0
        with self._content(spec.parent_asset_id, delegate) as stream:
            probe = validate_media(Path(stream.name), self.tools)
        if probe['kind'] != 'video':
            raise ValueError('variant planning requires video with a known source time origin and duration')
        video = next(s for s in probe['raw_metadata']['streams'] if s['index'] == probe['video_stream_index'])
        base = Fraction(video['time_base'])
        selected = spec.recipe.audio_stream_index
        tracks = [track['stream_index'] for track in probe['audio_tracks']]
        if spec.recipe.audio != 'drop':
            if selected is None:
                if len(tracks) > 1:
                    raise ValueError('multiple audio tracks require an explicit output track')
                selected = tracks[0] if tracks else None
            elif selected not in tracks:
                raise ValueError('selected audio stream does not exist')
        if len([s for s in probe['raw_metadata']['streams'] if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]) != 1:
            raise ValueError('variant planning currently requires one visual stream')
        origin, duration, offsets, durations = _stream_clock(probe, selected)
        clock = SourceClock(origin_seconds=origin, duration_seconds=duration,
                            time_base_numerator=base.numerator, time_base_denominator=base.denominator)
        time_map = FixedTimeMap(clock=clock, speed=spec.recipe.speed)
        if len({s.segment_id for s in spec.recipe.segments}) != len(spec.recipe.segments) or any(s.end_seconds > duration for s in spec.recipe.segments):
            raise ValueError('unique segments must be within the registered common source clock')
        data = dict(spec=spec.model_dump(mode='json'), source_sha256=parent['blob_sha256'],
                    root_asset_id=parent['root_asset_id'], time_map=time_map.model_dump(),
                    cumulative_speed=cumulative * spec.recipe.speed, selected_audio_stream=selected,
                    source_video_stream=probe['video_stream_index'], stream_offsets=offsets, stream_durations=durations,
                    source_display=[probe['display_width'], probe['display_height']])
        recipe_hash = sha256(encode({key: value for key, value in data.items() if key != 'spec'} | {'recipe': spec.recipe.model_dump()}).encode()).hexdigest()
        fingerprint = sha256(spec.model_dump_json().encode()).hexdigest()
        with self.database.transaction(write=True) as connection:
            parent = self.catalog._authorize(connection, spec.parent_asset_id, delegate)
            self.catalog._expected(parent, spec.expected_revision)
            old = connection.execute('SELECT * FROM variant_requests WHERE owner_id=? AND request_id=?', (principal.actor_id, request_id)).fetchone()
            if old and old['fingerprint'] != fingerprint:
                raise RevisionConflict('variant request ID was reused with different input')
            if old:
                return self._view(self._plan(connection, old['plan_id'], principal))
            plan = connection.execute('SELECT * FROM variant_plans WHERE owner_id=? AND parent_asset_id=? AND recipe_hash=?', (principal.actor_id, spec.parent_asset_id, recipe_hash)).fetchone()
            if plan is None:
                plan_id = f'plan-{uuid4().hex}'
                connection.execute('INSERT INTO variant_plans VALUES(?,?,?,?,?,?,?)', (plan_id, principal.actor_id, spec.parent_asset_id, parent['blob_sha256'], recipe_hash, encode(data), utc_now()))
                plan = self._plan(connection, plan_id, principal)
            connection.execute('INSERT INTO variant_requests VALUES(?,?,?,?)', (principal.actor_id, request_id, fingerprint, plan['plan_id']))
            return self._view(plan)

    def _execution(self, connection, plan, task_id, epoch, artifact):
        row = connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at,j.cancelled,j.owner_id
            FROM router_tasks t JOIN router_attempts a USING(task_id) JOIN router_jobs j USING(job_id)
            WHERE t.task_id=? AND a.lease_epoch=?''', (task_id, epoch)).fetchone()
        if row is None or not row['active'] or row['state'] != 'succeeded' or row['cancelled'] or row['lease_epoch'] != epoch or row['released_at'] is None:
            raise RevisionConflict('output execution is no longer current, successful and stopped')
        task = TaskEnvelope.model_validate_json(row['envelope_json'])
        result = WorkerResult.model_validate_json(row['result_json'])
        recipe = PlanSpec.model_validate_json(encode(json.loads(plan['spec_json'])['spec'])).recipe
        if recipe.renderer_revision is None or task.versions.preprocess_version != recipe.renderer_revision or task.versions.model_revision != recipe.model_revision:
            raise RevisionConflict('producer software/model versions do not match the fixed recipe')
        if result.status != 'succeeded':
            raise RevisionConflict('variant output requires a successful execution result')
        config = json.loads(connection.execute('SELECT config_json FROM router_attempts WHERE task_id=? AND lease_epoch=?', (task_id, epoch)).fetchone()[0])
        if task.stage not in self.output_stages or task.asset_id != plan['parent_asset_id'] or task.input_refs[0].sha256 != plan['source_sha256'] or row['owner_id'] != plan['owner_id']:
            raise AccessDenied('execution is not an authorized variant producer')
        if config.get('variant_plan_id') != plan['plan_id'] or config.get('variant_plan_hash') != plan['recipe_hash'] or artifact not in result.artifacts:
            raise RevisionConflict('output does not bind the fixed variant plan')
        self.router._artifacts(task, result)
        return task

    def register_output(self, plan_id, artifact, *, task_id, lease_epoch, principal):
        if principal.role != 'admin':
            raise AccessDenied('trusted result adoption capability required')
        with self.database.transaction() as connection:
            plan = self._plan(connection, plan_id, principal)
            task = self._execution(connection, plan, task_id, lease_epoch, artifact)
            data = json.loads(plan['spec_json'])
            recipe = PlanSpec.model_validate_json(encode(data['spec'])).recipe
        # Validate the exact output, never a caller URL/path or fallback original.
        with self.router.files.open_read(artifact.ref) as stream:
            digest = sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            if digest.hexdigest() != artifact.sha256:
                raise IntegrityError('variant output hash changed')
            stream.seek(0)
            validation = validate_media(Path(stream.name), self.tools, filename=f'output.{recipe.container}', full_decode=True)
            blob = self.catalog.object_store.ingest(stream)
        if blob.sha256 != artifact.sha256:
            raise IntegrityError('variant changed during ingest')
        mapping = FixedTimeMap.model_validate_json(encode(data['time_map']))
        video = next((s for s in validation['raw_metadata']['streams'] if s['index'] == validation['video_stream_index']), {})
        streams = validation['raw_metadata']['streams']
        if len([s for s in streams if s.get('codec_type') == 'video']) != 1 or video.get('codec_name') != recipe.codec or any(s.get('codec_type') not in ('video', 'audio') for s in streams):
            raise ValueError('output stream layout or codec is outside the fixed recipe')
        rate = Fraction(video.get('avg_frame_rate', '0/1'))
        tolerance = 0.02 + (1 / float(rate) if rate > 0 else 0.05)
        duration = validation['duration_seconds']
        audio_streams = [stream for stream in validation['raw_metadata']['streams'] if stream.get('codec_type') == 'audio']
        if len(audio_streams) != int(data['selected_audio_stream'] is not None):
            raise ValueError('output has an unexpected number of audio streams')
        audio_index = audio_streams[0]['index'] if audio_streams else None
        origin, duration, offsets, durations = _stream_clock(validation, audio_index)
        if validation['kind'] != 'video' or duration is None or abs(duration - mapping.expected_duration) > tolerance:
            raise ValueError('output duration does not match the fixed source time map')
        if [validation['display_width'], validation['display_height']] != data['source_display']:
            raise ValueError('output dimensions do not match the planned display')
        if recipe.audio == 'drop' and validation['has_audio'] or recipe.audio != 'drop' and bool(data['selected_audio_stream'] is not None) != validation['has_audio']:
            raise ValueError('output audio does not match the selected policy')
        if abs(origin) > tolerance:
            raise ValueError('output timeline was not normalized to its common origin')
        for source_index, output_index in ((data['source_video_stream'], validation['video_stream_index']), (data['selected_audio_stream'], audio_index)):
            if source_index is not None and (abs(offsets[str(output_index)] - data['stream_offsets'][str(source_index)] / recipe.speed) > tolerance or
                    abs(durations[str(output_index)] - data['stream_durations'][str(source_index)] / recipe.speed) > tolerance):
                raise ValueError('output stream timing does not preserve the planned common time base')
        with self._content(plan['parent_asset_id'], Principal(actor_id=plan['owner_id'])):
            pass
        with self.database.transaction(write=True) as connection:
            plan = self._plan(connection, plan_id, principal)
            self._execution(connection, plan, task_id, lease_epoch, artifact)
            old = connection.execute('SELECT asset_id,output_sha256 FROM variants WHERE plan_id=?', (plan_id,)).fetchone()
            if old:
                if old['output_sha256'] != blob.sha256:
                    raise RevisionConflict('a different output is already registered for this immutable plan')
                return self.catalog._snapshot(connection, self.catalog._authorize(connection, old['asset_id'], principal))
            parent = self.catalog._authorize(connection, plan['parent_asset_id'], Principal(actor_id=plan['owner_id']))
            now, asset_id = utc_now(), f'asset-{uuid4().hex}'
            connection.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)', (blob.sha256, blob.size_bytes, now))
            connection.execute('INSERT OR IGNORE INTO storage_locations VALUES(?,?,?)', (blob.sha256, self.catalog.backend_id, blob.object_key))
            if connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?', (blob.sha256,)).fetchone()[0] != blob.size_bytes:
                raise IntegrityError('registered output size conflict')
            if connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=? AND backend_id=?', (blob.sha256, self.catalog.backend_id)).fetchone()[0] != blob.object_key:
                raise IntegrityError('registered output location conflict')
            connection.execute("INSERT INTO assets(asset_id,blob_sha256,owner_id,name,revision,kind,root_asset_id,parent_asset_id,created_at) VALUES(?,?,?,?,1,'derived',?,?,?)",
                               (asset_id, blob.sha256, plan['owner_id'], 'Derived output', parent['root_asset_id'], parent['asset_id'], now))
            privacy_required=bool(recipe.mask or recipe.privacy_review_required or has_privacy_lineage(connection,parent))
            connection.execute('INSERT INTO variants VALUES(?,?,?,?,?,?,?,?,?,?,?)', (asset_id, plan_id, plan['source_sha256'], blob.sha256, plan['recipe_hash'], encode(data['time_map']), encode(validation), 'pending' if privacy_required else 'not_applicable', task_id, lease_epoch, now))
            source_ids = {row[0] for row in connection.execute('SELECT source_id FROM asset_sources WHERE asset_id=? UNION SELECT source_id FROM variant_source_links WHERE asset_id=?', (parent['asset_id'], parent['asset_id']))}
            for source_id in source_ids:
                connection.execute('INSERT INTO variant_source_links VALUES(?,?)', (asset_id, source_id))
            self.catalog._event(connection, asset_id, 1, principal, 'register_variant', {},
                                {'plan_id': plan_id, 'source_sha256': plan['source_sha256'], 'output_sha256': blob.sha256, 'tag_origin': 'not_inherited'})
            from media_tag.analysis.intake import ensure_intent
            ensure_intent(connection, asset_id, plan['owner_id'], content_kind=validation['kind'])
            return self.catalog._snapshot(connection, self.catalog._authorize(connection, asset_id, principal))

    def list_versions(self, asset_id, *, principal):
        _human(principal)
        with self.database.transaction() as connection:
            selected = self.catalog._authorize(connection, asset_id, principal)
            versions = []
            from media_tag.files.service import FileService
            files=FileService(self.router)
            for row in connection.execute('SELECT a.*,v.privacy_status,v.recipe_hash FROM assets a LEFT JOIN variants v USING(asset_id) WHERE root_asset_id=? AND deleted_at IS NULL ORDER BY created_at,asset_id', (selected['root_asset_id'],)):
                try:
                    self.catalog._authorize(connection, row['asset_id'], principal)
                except AccessDenied:
                    continue
                variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(row['asset_id'],)).fetchone()
                status=review_state(connection,variant) if variant else row['privacy_status']
                versions.append(dict(asset_id=row['asset_id'], parent_asset_id=row['parent_asset_id'], sha256=row['blob_sha256'],
                                     kind=row['kind'], privacy_status=status, recipe_hash=row['recipe_hash'],
                                     index_status=row['index_status'], download_available=bool(variant and status=='approved' and files._gate(connection,row) is None)))
            return versions
