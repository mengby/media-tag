from hashlib import sha256
from io import BytesIO
import json
import wave

import pytest
from fastapi.testclient import TestClient

from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.intake import ensure_intent
from media_tag.analysis.service import AnalysisService
from media_tag.api.app import create_app
from media_tag.catalog import AccessDenied, Principal
from media_tag.router.runtime import CPUHandlers, LocalExecutor, cpu_stages
from media_tag.similarity.pipeline import Handlers, stage
from media_tag.similarity.query import CandidateQuery, DescriptorQuery

from .conftest import picture


@pytest.fixture
def queued(authorized, tools):
    a = authorized
    e = a.env
    e.worker = e.workers[0]
    e.other = Principal(actor_id='synthetic-other')
    with e.database.transaction(write=True) as connection:
        connection.execute('UPDATE router_jobs SET cancelled=1')
    e.router.stages.update(cpu_stages(tools))
    e.router.stages['similarity'] = stage(tools)
    e.router.register_worker(e.worker.actor_id, ('probe', 'evidence', 'similarity'), principal=e.admin)
    analysis = AnalysisService(e.router, AnalysisDeployment(enabled=True))
    cpu, similarity = CPUHandlers(e.router, e.worker, tools), Handlers(e.router, e.worker, tools)

    def prepare(data, name, kind='image', principal=None):
        source = a.register(data, name=name, principal=principal)
        with e.database.transaction(write=True) as connection:
            ensure_intent(connection, source.asset_id, (principal or e.owner).actor_id, content_kind=kind)
        analysis.dispatch_pending(principal=principal or e.owner)
        return source

    def execute():
        executor = LocalExecutor(e.router, e.worker,
            lambda lease, stopped: similarity(lease, stopped) if lease.task.stage == 'similarity' else cpu(lease, stopped))
        for _ in range(32):
            if executor.run_once() is None:
                break
        return executor

    a.analysis, a.prepare, a.execute, a.query = analysis, prepare, execute, DescriptorQuery(e.router)
    return a


def test_real_queued_image_candidate_and_private_results(queued):
    a, e = queued, queued.env
    left = a.prepare(picture(), 'left.png')
    right = a.prepare(picture(size=80), 'right.png')
    a.execute()
    from media_tag.similarity.pipeline import verified
    asset, _, row = a.query._snapshot(left.asset_id, e.owner)
    verified(e.router, row, asset)
    assert a.query.status(left.asset_id, principal=e.owner)['state'] == 'ready'
    report = a.query.candidates(left.asset_id, CandidateQuery(asset_ids=(right.asset_id,)), principal=e.owner)
    assert report['items'][0].right.asset_id == right.asset_id
    assert report['items'][0].duplicate_confirmed is False
    assert 'jobs/' not in str(report) and 'source.bin' not in str(report)
    state = a.analysis.status(left.asset_id, principal=e.owner)
    assert state['analysis_complete'] is False


def test_hidden_pool_assets_and_worker_access(queued):
    a, e = queued, queued.env
    left = a.prepare(picture(), 'mine.png')
    hidden = a.prepare(picture(color='blue'), 'hidden.png', principal=e.other)
    a.execute()
    report = a.query.candidates(left.asset_id, CandidateQuery(asset_ids=(hidden.asset_id,)), principal=e.owner)
    assert hidden.asset_id not in str(report)
    with pytest.raises(AccessDenied):
        a.query.status(left.asset_id, principal=e.worker)


def test_current_report_tamper_is_unavailable(queued):
    a, e = queued, queued.env
    source = a.prepare(picture(), 'tampered.png')
    a.execute()
    with e.database.transaction() as connection:
        result = json.loads(connection.execute('''SELECT result_json FROM router_attempts a JOIN router_tasks t USING(task_id)
            JOIN router_jobs j USING(job_id) WHERE j.asset_id=? AND t.step_key='similarity' ''', (source.asset_id,)).fetchone()[0])
    ref = next(item for item in result['artifacts'] if item['kind'] == 'similarity_report')
    (e.workspace/ref['ref']).write_text('{}')
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'unavailable'


def test_new_pending_attempt_does_not_fall_back(queued):
    a, e = queued, queued.env
    source = a.prepare(picture(), 'pending.png')
    a.execute()
    state = a.analysis.status(source.asset_id, principal=e.owner)
    job = e.router.get_job(state['runtime']['job_id'], principal=e.owner)
    with e.database.transaction() as connection:
        from media_tag.router.models import StepSpec
        spec = StepSpec.model_validate_json(connection.execute("SELECT spec_json FROM router_tasks WHERE job_id=? AND step_key='similarity' AND active=1", (job['job_id'],)).fetchone()[0])
    e.router.replan(job['job_id'], (spec,), expected_revision=job['revision'], principal=e.owner)
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'queued'


def test_actual_audio_fingerprint_queued(queued):
    import math
    import struct
    stream = BytesIO()
    with wave.open(stream, 'wb') as audio:
        audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
        audio.writeframes(b''.join(struct.pack('<h', int(18000*math.sin(2*math.pi*440*i/16000))) for i in range(32000)))
    a, e = queued, queued.env
    source = a.prepare(stream.getvalue(), 'tone.wav', 'audio')
    a.execute()
    descriptor, state, _ = a.query.load(source.asset_id, principal=e.owner)
    assert state == 'ready' and descriptor.decoded_sample_count == 32000
    assert a.analysis.status(source.asset_id, principal=e.owner)['analysis_complete'] is False


def test_query_http_auth_body_and_no_client_descriptors(queued):
    a, e = queued, queued.env
    source = a.prepare(picture(), 'http.png')
    a.execute()
    app = create_app(e.router, {sha256(b'owner').hexdigest(): e.owner})
    client = TestClient(app)
    url = '/similarity/assets/'+source.asset_id
    assert client.get(url).status_code == 401
    headers = {'Authorization': 'Bearer owner'}
    assert client.get(url, headers=headers).json()['state'] == 'ready'
    assert client.post(url+'/candidates', headers=headers,
        json={'asset_ids': [source.asset_id], 'vectors': [[1, 0]]}).status_code == 422
    assert client.post(url+'/candidates', headers=headers, json={'asset_ids': [source.asset_id]*33}).status_code == 422


def test_cancelled_job_rejects_previous_success(queued):
    a, e = queued, queued.env
    source = a.prepare(picture(), 'cancel.png')
    a.execute()
    state = a.analysis.status(source.asset_id, principal=e.owner)
    e.router.cancel(state['runtime']['job_id'], principal=e.owner)
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'inactive_job'


def test_request_byte_budget_prevents_hashing_whole_pool(queued):
    a, e = queued, queued.env
    source = a.prepare(picture(), 'budget.png')
    a.execute()
    descriptor, state, used = a.query.load(source.asset_id, principal=e.owner, remaining=1)
    assert descriptor is None and state == 'request_byte_budget' and used == 0


def test_actual_animation_all_frames_and_video_sparse_scope(queued, tools):
    from PIL import Image
    stream = BytesIO()
    Image.new('RGB', (32, 32), 'red').save(stream, format='GIF', save_all=True,
        append_images=[Image.new('RGB', (32, 32), 'blue')], duration=[80, 120], loop=0)
    a, e = queued, queued.env
    gif = a.prepare(stream.getvalue(), 'animated.gif', 'animation')
    video, _ = tools.ffmpeg_run(['-f', 'lavfi', '-i', 'color=red:s=64x64:r=4:d=1', '-an', '-c:v', 'libx264',
        '-movflags', 'frag_keyframe+empty_moov', '-f', 'mp4', 'pipe:1'], timeout=10)
    clip = a.prepare(video, 'clip.mp4', 'video')
    a.execute()
    animation, state, _ = a.query.load(gif.asset_id, principal=e.owner)
    assert state == 'ready' and animation.frame_durations_ms == (80., 120.)
    visual, state, _ = a.query.load(clip.asset_id, principal=e.owner)
    assert state == 'ready' and visual.coverage == 'sparse_decoded_video_frames' and not visual.full_video_observed


def test_unknown_type_probe_adoption_appends_similarity_without_rerun(queued):
    a, e = queued, queued.env
    source = a.prepare(picture(), 'misleading.wav', None)
    a.execute()
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'not_analyzed'
    a.analysis.dispatch_pending(principal=e.owner)
    a.execute()
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'ready'
    with e.database.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM router_attempts a JOIN router_tasks t USING(task_id) JOIN router_jobs j USING(job_id) WHERE j.asset_id=? AND t.step_key='probe'", (source.asset_id,)).fetchone()[0] == 1


def test_repair_keeps_probe_and_rebuilds_damaged_similarity(queued):
    from media_tag.analysis.repair import RepairSubmission, repair
    a, e = queued, queued.env
    source = a.prepare(picture(), 'repair.png')
    a.execute()
    asset, _, row = a.query._snapshot(source.asset_id, e.owner)
    ref = next(item for item in json.loads(row['result_json'])['artifacts'] if item['kind'] == 'similarity_input')
    (e.workspace/ref['ref']).write_bytes(b'changed')
    state = a.analysis.status(source.asset_id, principal=e.owner)
    submission = RepairSubmission(request_id='repair-similarity', expected_asset_revision=asset['revision'],
        source_sha256=asset['blob_sha256'], expected_job_revision=state['runtime']['revision'])
    result = repair(a.analysis, source.asset_id, submission, principal=e.owner)
    assert result['patched_roots'] == ['similarity'] and 'probe' in result['reused_stage_keys']
    a.execute()
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'ready'


def test_configuration_change_invalidates_descriptor(queued):
    from dataclasses import replace
    a, e = queued, queued.env
    source = a.prepare(picture(), 'version.png')
    a.execute()
    definition = e.router.stages['similarity']
    e.router.stages['similarity'] = replace(definition, implementation_version=definition.implementation_version+'changed')
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'unavailable'


def test_privacy_gate_hides_previously_analyzed_pool_member(queued, monkeypatch):
    from media_tag.files.service import FileService
    a, e = queued, queued.env
    left = a.prepare(picture(), 'private-left.png')
    right = a.prepare(picture(size=80), 'private-right.png')
    a.execute()
    original = FileService._gate
    monkeypatch.setattr(FileService, '_gate', lambda self, connection, asset, **kwargs:
        'privacy_review_pending' if asset['asset_id'] == right.asset_id else original(self, connection, asset, **kwargs))
    report = a.query.candidates(left.asset_id, CandidateQuery(asset_ids=(right.asset_id,)), principal=e.owner)
    assert right.asset_id not in str(report)


def test_source_revision_change_can_be_repaired_without_reprobing(queued):
    from media_tag.analysis.repair import RepairSubmission, repair
    a, e = queued, queued.env
    source = a.prepare(picture(), 'revision.png')
    a.execute()
    e.catalog.change_access(source.asset_id, principal=e.owner, expected_revision=source.asset_revision,
        subject_id=e.other.actor_id, permission='read', reason='Synthetic sharing revision')
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'source_revision_changed'
    asset = e.catalog.get_asset(source.asset_id, principal=e.owner)
    state = a.analysis.status(source.asset_id, principal=e.owner)
    result = repair(a.analysis, source.asset_id, RepairSubmission(request_id='revision-repair',
        expected_asset_revision=asset.revision, source_sha256=asset.sha256, expected_job_revision=state['runtime']['revision']), principal=e.owner)
    assert result['stage_assessments']['similarity'] == 'incompatible'
    assert 'probe' in result['reused_stage_keys']
    a.execute()
    assert a.query.status(source.asset_id, principal=e.other)['state'] == 'ready'


def test_single_frame_gif_retains_static_descriptor_and_repair_is_stable(queued):
    from media_tag.analysis.repair import RepairSubmission, repair
    a, e = queued, queued.env
    source = a.prepare(picture(format='GIF'), 'single.gif', 'animation')
    a.execute()
    from media_tag.similarity.pipeline import verified
    asset, _, row = a.query._snapshot(source.asset_id, e.owner)
    verified(e.router, row, asset)
    descriptor, state, _ = a.query.load(source.asset_id, principal=e.owner)
    assert state == 'ready' and descriptor.media_kind == 'image' and descriptor.coverage == 'complete_image'
    asset = e.catalog.get_asset(source.asset_id, principal=e.owner)
    runtime = a.analysis.status(source.asset_id, principal=e.owner)['runtime']
    result = repair(a.analysis, source.asset_id, RepairSubmission(request_id='single-gif-repair',
        expected_asset_revision=asset.revision, source_sha256=asset.sha256, expected_job_revision=runtime['revision']), principal=e.owner)
    assert result['stage_assessments']['similarity'] == 'valid' and result['patched_roots'] == []


@pytest.mark.parametrize('kind', ['similarity_report', 'similarity_input'])
def test_appended_artifact_rejected_before_read_and_repair_avoids_unbounded_hash(queued, monkeypatch, kind):
    from media_tag.analysis.repair import RepairSubmission, repair
    a, e = queued, queued.env
    source = a.prepare(picture(), 'oversize.png')
    a.execute()
    asset, _, row = a.query._snapshot(source.asset_id, e.owner)
    ref = next(item for item in json.loads(row['result_json'])['artifacts'] if item['kind'] == kind)
    with (e.workspace/ref['ref']).open('ab') as stream:
        stream.write(b'x'*(5*1024**2 if kind == 'similarity_report' else 4096))
    original = e.router.files.open_read
    reads = []

    class GuardedRead:
        def __init__(self, manager): self.manager = manager
        def __enter__(self):
            self.stream = self.manager.__enter__()
            return self
        def __exit__(self, *args): return self.manager.__exit__(*args)
        def fileno(self): return self.stream.fileno()
        def read(self, *args):
            reads.append(args)
            raise AssertionError('oversize artifact should be refused before reading')

    monkeypatch.setattr(e.router.files, 'open_read', lambda path: GuardedRead(original(path)) if path == ref['ref'] else original(path))
    assert a.query.status(source.asset_id, principal=e.owner)['state'] == 'unavailable'
    runtime = a.analysis.status(source.asset_id, principal=e.owner)['runtime']
    repaired = repair(a.analysis, source.asset_id, RepairSubmission(request_id='oversize-repair',
        expected_asset_revision=asset['revision'], source_sha256=asset['blob_sha256'], expected_job_revision=runtime['revision']), principal=e.owner)
    assert repaired['patched_roots'] == ['similarity'] and not reads
