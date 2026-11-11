from hashlib import sha256
from io import BytesIO
import json
import os
import wave

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.analysis.repair import RepairSubmission, repair
from media_tag.api.app import create_app
from media_tag.precision.contracts import PrecisionDeployment
from media_tag.precision.service import dispatch_pending, request_precision
from media_tag.analysis.service import AnalysisService

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Precision runner containment requires Windows Job Objects')


def status(p, asset_id=None):
    return p.analysis.status(asset_id or p.sought.asset.asset_id, principal=p.env.owner)


def result_file(p, task_id):
    with p.env.database.transaction() as connection:
        row = connection.execute('SELECT result_json FROM router_attempts WHERE task_id=? ORDER BY lease_epoch DESC LIMIT 1', (task_id,)).fetchone()
    return json.loads(row[0])


def test_real_bridge_process_runs_ten_field_protocol_and_preserves_human_values(precise):
    p = precise
    before = p.env.catalog.get_asset(p.sought.asset.asset_id, principal=p.env.owner)
    deploy = p.deployment()
    spec, accepted, executor = p.submit(deploy)
    assert not accepted['model_capability_acceptance']
    assert executor.run_once()['accepted']
    view = status(p)
    assert view['runtime']['state'] == 'succeeded'
    assert view['analysis_complete'] and view['state'] == 'analysis_draft_ready'
    document = view['topic_analysis']
    assert len(document['fields']) == 10 and all(f['origin'] == 'precision' for f in document['fields'])
    assert not document['medical_confirmation'] and not document['privacy_confirmation']
    assert not document['effective_values_written']
    assert {f['field'] for f in document['fields'] if f['requires_field_review']} == {'科室', '器官/系统', '疾病/症状', '过程/状态'}
    after = p.env.catalog.get_asset(before.asset_id, principal=p.env.owner)
    assert after.revision == before.revision and after.effective_values == before.effective_values
    again = request_precision(p.analysis, deploy, before.asset_id, spec, principal=p.env.owner)
    assert again['reused_request'] and again['task_id'] == accepted['task_id']
    assert 'adapter_path' not in json.dumps(document) and 'jobs/' not in json.dumps(document)
    result = result_file(p, accepted['task_id'])
    report = next(a for a in result['artifacts'] if a['kind'] == 'precision_report')
    raw = json.loads((p.env.workspace/report['ref']).read_text(encoding='utf8'))
    assert raw['provider']['model_id'] == 'synthetic-not-a-model'


@pytest.mark.parametrize('mode', ['forged_approval', 'wrong_source'])
def test_bridge_cannot_forge_approval_or_bind_reply_to_another_source(precise, mode):
    p = precise
    _, _, executor = p.submit(p.deployment(mode))
    assert executor.run_once()['accepted']
    view = status(p)
    assert not view['analysis_complete']
    assert all(f['origin'] != 'precision' for f in view['topic_analysis']['fields'])
    assert view['runtime']['state'] in ('failed', 'needs_attention')


def test_unpublished_medical_candidate_is_preserved_without_becoming_formal_value(precise):
    p = precise
    _, _, executor = p.submit(p.deployment('outside'), fields=('疾病/症状',))
    executor.run_once()
    field = next(f for f in status(p)['topic_analysis']['fields'] if f['field'] == '疾病/症状')
    assert field['value']['state'] == 'no_match' and field['next_step'] == 'vocabulary'
    assert field['candidates'] == ['合成未发布候选'] and field['requires_field_review']


def test_timeout_and_changed_bridge_are_failures_without_lease_resource_leak(precise):
    p = precise
    _, accepted, executor = p.submit(p.deployment('sleep', timeout_seconds=3))
    assert executor.run_once()['accepted']
    assert status(p)['runtime']['state'] in ('failed', 'needs_attention')
    with p.env.database.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM router_holds WHERE task_id=?', (accepted['task_id'],)).fetchone()[0] == 0
    deploy = p.deployment()
    _, accepted, executor = p.submit(deploy, request='changed-code')
    p.bridge.write_text(p.bridge.read_text(encoding='utf8')+'\n# changed\n', encoding='utf8')
    assert executor.run_once()['accepted']
    result = result_file(p, accepted['task_id'])
    assert result['status'] == 'failed' and result['errors'][0]['code'] == 'precision_bridge_integrity'


def test_new_field_scope_shadows_old_success_only_for_that_field_and_report_repair_works(precise):
    p = precise
    deploy = p.deployment()
    _, initial, executor = p.submit(deploy)
    executor.run_once()
    assert status(p)['analysis_complete']
    _, _, executor = p.submit(deploy, fields=('画面主体',), request='subset')
    fields = {f['field']: f for f in status(p)['topic_analysis']['fields']}
    assert fields['画面主体']['value']['state'] == 'unknown'
    assert fields['英文检索词']['origin'] == 'precision'
    executor.run_once()
    assert status(p)['analysis_complete']
    result = result_file(p, initial['task_id'])
    ref = next(a for a in result['artifacts'] if a['kind'] == 'precision_report')
    (p.env.workspace/ref['ref']).write_text('{}')
    view = status(p)
    assert not view['analysis_complete']
    assert next(f for f in view['topic_analysis']['fields'] if f['field'] == '画面主体')['value']['state'] == 'observed'
    asset = p.env.catalog.get_asset(p.sought.asset.asset_id, principal=p.env.owner)
    repaired = repair(p.analysis, asset.asset_id, RepairSubmission(request_id='repair-precision',
        expected_asset_revision=asset.revision, source_sha256=asset.sha256,
        expected_job_revision=view['runtime']['revision']), principal=p.env.owner)
    assert len(repaired['patched_roots']) == 1 and repaired['patched_roots'][0].startswith('precision-')
    assert executor.run_once()['accepted']
    assert status(p)['analysis_complete']


def test_pure_audio_uses_actual_bytes_and_only_eight_applicable_provider_fields(precise):
    p = precise
    stream = BytesIO()
    with wave.open(stream, 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000); out.writeframes(b'\0\0'*4000)
    asset = p.register(stream.getvalue(), 'synthetic.wav', 'audio')
    _, accepted, executor = p.submit(p.deployment('unknown'), asset_id=asset.asset_id)
    assert len(accepted['fields']) == 8
    executor.run_once()
    view = status(p, asset.asset_id)
    assert not view['analysis_complete']
    fields = {f['field']: f for f in view['topic_analysis']['fields']}
    assert fields['画面主体']['value']['state'] == fields['表现形式']['value']['state'] == 'not_applicable'
    assert fields['情绪/语气']['origin'] == 'precision' and fields['情绪/语气']['next_step'] == 'human'
    assert view['topic_analysis']['evidence'][0]['kind'] == 'audio_segment'
    assert view['topic_analysis']['evidence'][0]['artifact_sha256'] == asset.sha256


def test_transparent_image_bytes_are_preserved_for_actual_bridge_input(precise):
    p = precise
    data = BytesIO(); Image.new('RGBA', (8, 8), (255, 0, 0, 80)).save(data, 'PNG')
    asset = p.register(data.getvalue(), 'synthetic-alpha.png', 'image')
    _, accepted, executor = p.submit(p.deployment(), asset_id=asset.asset_id)
    executor.run_once()
    result = result_file(p, accepted['task_id'])
    evidence = result['evidence'][0]
    raw = (p.env.workspace/evidence['artifact']['ref']).read_bytes()
    assert raw == data.getvalue() and sha256(raw).hexdigest() == asset.sha256


def test_opt_in_dispatch_waits_for_local_work_and_never_retries_existing_precision(precise):
    p = precise
    deploy = p.deployment(auto_dispatch=True)
    assert len(dispatch_pending(p.analysis, deploy, principal=p.env.owner)) == 1
    assert dispatch_pending(p.analysis, deploy, principal=p.env.owner) == []


def test_http_precision_request_uses_existing_auth_and_strict_field_contract(precise):
    p = precise
    deploy = p.deployment()
    app = create_app(p.env.router, {sha256(name.encode()).hexdigest(): person for name, person in
        [('owner', p.env.owner), ('worker', p.env.worker), ('other', p.env.other)]},
        analysis_options=p.analysis.options, precision_options=deploy)
    client = TestClient(app)
    path = '/analysis/assets/'+p.sought.asset.asset_id+'/precision'
    asset = p.env.catalog.get_asset(p.sought.asset.asset_id, principal=p.env.owner)
    data = {'request_id': 'http-precision', 'source_sha256': asset.sha256,
        'expected_asset_revision': asset.revision, 'expected_job_revision': status(p)['runtime']['revision'], 'fields': list(TOPIC_FIELDS)}
    assert client.post(path, json=data).status_code == 401
    assert client.post(path, json=data, headers={'Authorization': 'Bearer worker'}).status_code == 403
    assert client.post(path, json=data, headers={'Authorization': 'Bearer other'}).status_code == 403
    response = client.post(path, json=data, headers={'Authorization': 'Bearer owner'})
    assert response.status_code == 200 and response.json()['fields'] == list(TOPIC_FIELDS)
    assert client.post(path, json=data, headers={'Authorization': 'Bearer owner'}).json()['reused_request']


def test_timeout_stops_bridge_grandchildren_before_resource_release(precise):
    import ctypes
    from ctypes import wintypes
    p = precise
    _, accepted, executor = p.submit(p.deployment('childsleep', timeout_seconds=4))
    assert executor.run_once()['accepted']
    pid = int(p.bridge.with_suffix('.pid').read_text())
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100000, False, pid)
    if handle:
        try:
            assert kernel.WaitForSingleObject(handle, 0) == 0
        finally:
            kernel.CloseHandle(handle)
    with p.env.database.transaction() as connection:
        assert not connection.execute('SELECT * FROM router_holds WHERE task_id=?', (accepted['task_id'],)).fetchall()


@pytest.mark.parametrize('kind', ['video', 'animation'])
def test_continuous_media_input_is_the_complete_original_not_a_sample_frame(precise, tmp_path, kind):
    p = precise
    if kind == 'video':
        path = tmp_path/'synthetic-video.mp4'
        p.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=32x24:r=5', '-t', '0.6',
            '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(path)], timeout=10.0)
        data, name = path.read_bytes(), 'synthetic-video.mp4'
    else:
        out = BytesIO()
        images = [Image.new('RGBA', (8, 8), color) for color in ((255, 0, 0, 0), (0, 0, 255, 255))]
        images[0].save(out, format='GIF', save_all=True, append_images=images[1:], duration=200, loop=0, transparency=0)
        data, name = out.getvalue(), 'synthetic-animation.gif'
    asset = p.register(data, name, kind)
    _, accepted, executor = p.submit(p.deployment(), asset_id=asset.asset_id)
    assert executor.run_once()['accepted']
    result = result_file(p, accepted['task_id'])
    assert result['status'] == 'succeeded', result['errors']
    evidence = result['evidence'][0]
    assert evidence['kind'] == 'video_segment' and evidence['start_seconds'] == 0.0 and evidence['end_seconds'] > 0.0
    assert (p.env.workspace/evidence['artifact']['ref']).read_bytes() == data


def test_stream_duration_is_used_instead_of_nonzero_container_endpoint(precise, tmp_path):
    p = precise
    base, shifted = tmp_path/'base.mp4', tmp_path/'shifted.mp4'
    p.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=32x24:r=5', '-t', '0.6',
        '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(base)], timeout=10.0)
    p.tools.ffmpeg_run(['-v', 'error', '-i', str(base), '-c', 'copy', '-output_ts_offset', '5', str(shifted)], timeout=10.0)
    asset = p.register(shifted.read_bytes(), 'synthetic-offset.mp4', 'video')
    _, accepted, executor = p.submit(p.deployment(max_duration_seconds=1.0), asset_id=asset.asset_id)
    assert executor.run_once()['accepted']
    result = result_file(p, accepted['task_id'])
    assert result['status'] == 'succeeded', result['errors']
    assert result['evidence'][0]['end_seconds'] == pytest.approx(.6)


def test_missing_provider_and_context_budget_remain_explicit_failures(precise):
    from media_tag.catalog import RevisionConflict
    p = precise
    asset = p.env.catalog.get_asset(p.sought.asset.asset_id, principal=p.env.owner)
    from media_tag.precision.service import PrecisionSubmission
    spec = PrecisionSubmission(request_id='unselected', expected_asset_revision=asset.revision,
        source_sha256=asset.sha256, expected_job_revision=status(p)['runtime']['revision'])
    with pytest.raises(RevisionConflict, match='provider unavailable'):
        request_precision(p.analysis, PrecisionDeployment(), asset.asset_id, spec, principal=p.env.owner)
    _, accepted, executor = p.submit(p.deployment(max_request_bytes=1024))
    assert executor.run_once()['accepted']
    result = result_file(p, accepted['task_id'])
    assert result['status'] == 'failed' and result['errors'][0]['code'] == 'precision_request_budget'
    fields = status(p)['topic_analysis']['fields']
    assert all(field['value']['state'] == 'unknown' and field['next_step'] == 'provider' for field in fields)


def test_auto_dispatch_fills_only_missing_field_scopes_and_model_change_does_not_rerun_library(precise):
    p = precise
    deploy = p.deployment(auto_dispatch=True)
    _, _, executor = p.submit(deploy, fields=('画面主体',))
    executor.run_once()
    outcomes = dispatch_pending(p.analysis, deploy, principal=p.env.owner)
    assert len(outcomes) == 1 and len(outcomes[0]['fields']) == 9 and '画面主体' not in outcomes[0]['fields']
    assert executor.run_once()['accepted']
    assert status(p)['analysis_complete']
    assert dispatch_pending(p.analysis, p.deployment('unknown', auto_dispatch=True), principal=p.env.owner) == []


def test_bounded_auto_cursor_survives_executor_recreation(precise, monkeypatch):
    p = precise
    other = p.register(p.sought.original.read_bytes(), 'synthetic-second.png', 'image')
    deploy = p.deployment(auto_dispatch=True, dispatch_limit=1)
    original_gate = p.analysis.files._gate
    monkeypatch.setattr(p.analysis.files, '_gate', lambda connection, asset:
        'privacy_review_pending' if asset['asset_id'] == p.sought.asset.asset_id else original_gate(connection, asset))
    assert dispatch_pending(p.analysis, deploy, principal=p.env.owner) == []
    recreated = AnalysisService(p.env.router, p.analysis.options)
    outcomes = dispatch_pending(recreated, deploy, principal=p.env.owner)
    assert len(outcomes) == 1 and outcomes[0]['asset_id'] == other.asset_id


def test_configured_worker_dispatches_and_executes_selected_bridge(precise, tmp_path):
    from pathlib import Path
    from media_tag.api.config import LocalConfig, executor_from_config
    p = precise
    deploy = p.deployment(auto_dispatch=True)
    config = LocalConfig(workspace=str(p.env.workspace), database=str(p.env.database.path),
        object_store=str(p.env.catalog.object_store.root), template_dir=str((Path.cwd()/'asset-index-template').resolve()),
        template_fingerprint=p.env.contract.fingerprint, credentials={sha256(b'owner').hexdigest(): p.env.owner,
        sha256(b'worker').hexdigest(): p.env.worker}, workers=(p.env.worker.actor_id,), tool_dir=str(p.tools.ffmpeg.parent),
        analysis=p.analysis.options, precision=deploy, review=p.sought.reviews.config)
    path = tmp_path/'synthetic-precision.local.json'
    path.write_text(config.model_dump_json(), encoding='utf8')
    executor = executor_from_config(path, p.env.worker.actor_id)
    assert executor.run_once()['accepted']
    assert status(p)['analysis_complete']
    assert executor.run_once() is None


def test_policy_change_waits_for_new_precision_configuration_instead_of_retrying_old_spec(precise):
    from media_tag.review.service import ReviewService
    from media_tag.review.models import ReviewDeployment
    p = precise
    deploy = p.deployment()
    _, accepted, executor = p.submit(deploy)
    executor.run_once()
    ReviewService(p.env.router, ReviewDeployment(enabled=True, reviewers=(p.env.owner.actor_id,)))
    view = status(p)
    assert not view['analysis_complete']
    asset = p.env.catalog.get_asset(p.sought.asset.asset_id, principal=p.env.owner)
    repaired = repair(p.analysis, asset.asset_id, RepairSubmission(request_id='changed-policy',
        source_sha256=asset.sha256, expected_asset_revision=asset.revision,
        expected_job_revision=view['runtime']['revision']), principal=p.env.owner)
    assert not repaired['patched_roots']
    assert 'awaiting_current_precision_configuration' in repaired['stage_assessments'].values()
    _, replacement, executor = p.submit(deploy, request='new-policy-request')
    assert replacement['task_id'] != accepted['task_id']
    assert executor.run_once()['accepted'] and status(p)['analysis_complete']


def test_containment_assignment_failure_kills_unassigned_runner_promptly(precise, monkeypatch):
    import time
    from media_tag.models.contracts import ModelFailure
    p = precise
    _, accepted, executor = p.submit(p.deployment(timeout_seconds=4))
    children = []
    def fail_assign(tree, process):
        children.append(process)
        raise ModelFailure('process_tree_containment_failed')
    monkeypatch.setattr('media_tag.precision.runtime.ProcessTree.assign', fail_assign)
    started = time.monotonic()
    assert executor.run_once()['accepted']
    assert time.monotonic()-started < 2.0
    assert children and all(child.poll() is not None for child in children)
    assert result_file(p, accepted['task_id'])['errors'][0]['code'] == 'process_tree_containment_failed'


@pytest.mark.parametrize('corruption', ['origin', 'versions', 'whole_input'])
def test_adoption_and_repair_share_identical_field_and_whole_input_qualification(precise, corruption):
    from media_tag.precision.projection import verified
    from media_tag.analysis.repair import _stage_state
    from media_tag.router.models import StepSpec
    p = precise
    _, accepted, executor = p.submit(p.deployment())
    executor.run_once()
    with p.env.database.transaction() as connection:
        row = dict(connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at
            FROM router_tasks t JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.task_id=?''', (accepted['task_id'],)).fetchone())
    result = json.loads(row['result_json'])
    report_ref = next(ref for ref in result['artifacts'] if ref['kind'] == 'precision_report')
    report_path = p.env.workspace/report_ref['ref']
    report = json.loads(report_path.read_text(encoding='utf8'))
    subject = next(field for field in report['document']['fields'] if field['field'] == '画面主体')
    if corruption == 'origin':
        subject['origin'] = 'local'
    elif corruption == 'versions':
        subject['versions']['model_revision'] = 'unrelated-version'
    else:
        input_ref = next(ref for ref in result['artifacts'] if ref['kind'] == 'precision_input')
        data = BytesIO(); Image.new('RGB', (24, 24), 'blue').save(data, 'PNG')
        (p.env.workspace/input_ref['ref']).write_bytes(data.getvalue())
        digest = sha256(data.getvalue()).hexdigest()
        input_ref['sha256'] = digest
        result['evidence'][0]['artifact']['sha256'] = digest
        report['document']['evidence'][0]['artifact']['sha256'] = digest
    report_path.write_text(json.dumps(report), encoding='utf8')
    report_ref['sha256'] = sha256(report_path.read_bytes()).hexdigest()
    row['result_json'] = json.dumps(result)
    asset = {'asset_id': p.sought.asset.asset_id, 'blob_sha256': p.sought.asset.sha256}
    with pytest.raises(ValueError):
        verified(p.env.router, row, asset, 'image')
    assert _stage_state(p.analysis, row, StepSpec.model_validate_json(row['spec_json']), p.sought.asset.sha256, 'image') == 'unavailable'
