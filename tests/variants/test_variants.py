from hashlib import sha256
import json
import sqlite3

from pydantic import ValidationError
import pytest

from media_tag.catalog import AccessDenied, Principal, RevisionConflict
from media_tag.storage import IntegrityError
from media_tag.upload.models import UploadSpec
from media_tag.variants.models import FixedTimeMap, MaskSpec, PlanSpec, Recipe, SourceClock
from media_tag.variants.service import VariantService
from media_tag.storage.filesystem import FilesystemBackend
from .conftest import plan, produced, register


@pytest.mark.parametrize('payload', [{'speed': 0}, {'speed': 5}, {'speed': True}, {'ffmpeg': '-evil shell'}, {'audio': 'drop', 'audio_stream_index': 1}, {'crf': 100}])
def test_recipe_is_strict_and_whitelisted(payload):
    with pytest.raises(ValidationError):
        Recipe.model_validate_json(json.dumps(payload))


def test_default_speed_source_clock_and_normalized_mapping():
    assert Recipe().speed == 1.0
    clock = SourceClock(origin_seconds=5.0, duration_seconds=10.0, time_base_numerator=1, time_base_denominator=90000)
    mapping = FixedTimeMap(clock=clock, speed=2.0)
    assert mapping.expected_duration == 5 and mapping.source_to_output(4) == 2
    with pytest.raises(ValueError):
        mapping.source_to_output(11)


def test_plan_deduplication_immutable_source_and_request_conflict(variants):
    env = variants
    before = env.parent_path.read_bytes()
    first = plan(env, recipe=Recipe(speed=2.0))
    assert plan(env, recipe=Recipe(speed=2.0))['plan_id'] == first['plan_id']
    assert plan(env, request='repeat', recipe=Recipe(speed=2.0))['plan_id'] == first['plan_id']
    with pytest.raises(RevisionConflict):
        plan(env, recipe=Recipe(speed=1.0))
    assert env.parent_path.read_bytes() == before
    assert first['contract']['time_map']['clock']['time_base_denominator'] > 0
    updated = plan(env, request='new-software', recipe=Recipe(speed=2.0, renderer_revision='synthetic-output-v2'))
    assert updated['recipe_hash'] != first['recipe_hash']


def test_private_output_lineage_source_links_no_tags_and_exact_repeat(variants):
    env = variants
    planned = plan(env, recipe=Recipe(speed=2.0, mask=MaskSpec(revision='synthetic-mask-v1')))
    output = produced(env, planned)
    item = register(env, planned, output)
    assert register(env, planned, output).asset_id == item.asset_id
    assert item.parent_asset_id == env.parent.asset_id and item.root_asset_id == env.parent.asset_id
    assert item.sha256 == output.artifact.sha256 and not item.effective_values and not item.suggestions
    with env.database.transaction() as connection:
        row = connection.execute('SELECT * FROM variants').fetchone()
        assert row['privacy_status'] == 'pending' and json.loads(row['validation_json'])['intake_decode'] == 'full'
        assert connection.execute('SELECT count(*) FROM variant_source_links').fetchone()[0] == 1
    with pytest.raises(AccessDenied):
        with env.catalog.open_content(item.asset_id, principal=env.owner):
            pass
    versions = env.variants.list_versions(item.asset_id, principal=env.owner)
    assert len(versions) == 2 and all(not item['download_available'] for item in versions)
    from media_tag.analysis.service import AnalysisService
    state = AnalysisService(env.router).status(item.asset_id, principal=env.owner)
    assert state['state'] == 'awaiting_file_gate' and not state['analysis_complete']
    assert state['topic_analysis'] is None
    from media_tag.analysis.deployment import AnalysisDeployment
    analysis = AnalysisService(env.router, AnalysisDeployment(enabled=True))
    outcomes = analysis.dispatch_pending(principal=env.owner)
    assert any(row['intent_id'] == state['intent_id'] and row['state'] == 'awaiting_file_gate' for row in outcomes)
    assert not analysis.status(item.asset_id, principal=env.owner)['runtime_dispatched']
    from media_tag.analysis.repair import RepairSubmission, repair
    from media_tag.catalog import RevisionConflict
    pending_asset = env.catalog.get_asset(item.asset_id, principal=env.owner)
    with pytest.raises(RevisionConflict):
        repair(analysis, item.asset_id, RepairSubmission(request_id='pending-privacy-repair',
            expected_asset_revision=pending_asset.revision, source_sha256=pending_asset.sha256,
            expected_job_revision=1), principal=env.owner)


def test_derived_input_requires_explicit_chain_and_records_cumulative_speed(variants):
    env = variants
    first = plan(env, recipe=Recipe(speed=2.0)); item = register(env, first, produced(env, first))
    with pytest.raises(ValueError, match='explicit'):
        plan(env, request='implicit', parent=item, recipe=Recipe(speed=2.0))
    chained = plan(env, request='explicit', parent=item, recipe=Recipe(speed=2.0), chain=True)
    assert chained['contract']['cumulative_speed'] == 4.0
    baseline = plan(env, request='new-baseline', recipe=Recipe(speed=1.5))
    assert baseline['parent_asset_id'] == env.parent.asset_id and baseline['contract']['cumulative_speed'] == 1.5


@pytest.mark.parametrize('issue', ['cancelled', 'unaccepted_artifact', 'tampered', 'wrong_duration', 'worker_publish', 'disabled_producer', 'extra_video_stream'])
def test_invalid_or_untrusted_outputs_cannot_register(variants, issue):
    env = variants
    planned = plan(env)
    output = produced(env, planned, duration=0.3 if issue == 'wrong_duration' else None, extra_video=issue == 'extra_video_stream')
    if issue == 'cancelled':
        env.router.cancel(output.job['job_id'], principal=env.owner)
    elif issue == 'tampered':
        output.path.write_bytes(b'corrupt')
    elif issue == 'unaccepted_artifact':
        from media_tag.contracts.models import ArtifactRef
        output.artifact = ArtifactRef(kind='another-kind', ref=output.artifact.ref, sha256=output.artifact.sha256)
    elif issue == 'disabled_producer':
        env.variants = VariantService(env.router, env.variants.tools)
    with pytest.raises((AccessDenied, RevisionConflict, IntegrityError, ValueError)):
        if issue == 'worker_publish':
            env.variants.register_output(planned['plan_id'], output.artifact, task_id=output.lease.task.task_id, lease_epoch=1, principal=env.workers[0])
        else:
            register(env, planned, output)
    with env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM variants').fetchone()[0] == 0


def test_variant_publication_rolls_back_on_source_link_failure(variants):
    env = variants
    planned = plan(env); output = produced(env, planned)
    with env.database.transaction(write=True) as connection:
        connection.execute("CREATE TRIGGER synthetic_link_failure BEFORE INSERT ON variant_source_links BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):
        register(env, planned, output)
    with env.database.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM assets WHERE kind='derived'").fetchone()
        connection.execute('DROP TRIGGER synthetic_link_failure')
    assert register(env, planned, output).parent_asset_id == env.parent.asset_id


def test_version_list_filters_each_objects_permissions_and_pending_content(variants):
    env = variants
    planned = plan(env, recipe=Recipe(mask=MaskSpec(revision='synthetic-v1')))
    item = register(env, planned, produced(env, planned))
    reader = Principal(actor_id='synthetic-variant-reader')
    with pytest.raises(AccessDenied):
        env.variants.list_versions(item.asset_id, principal=reader)
    env.catalog.change_access(item.asset_id, principal=env.owner, expected_revision=1, subject_id=reader.actor_id, permission='read', reason='synthetic grant')
    listing = env.variants.list_versions(item.asset_id, principal=reader)
    assert [row['asset_id'] for row in listing] == [item.asset_id]
    with pytest.raises(AccessDenied):
        env.variants.create_plan(PlanSpec(parent_asset_id=item.asset_id, expected_revision=2, chain_from_derived=True), request_id='reader-plan', principal=reader)
    with pytest.raises(AccessDenied):
        env.variants.list_versions(item.asset_id, principal=env.workers[0])


def upload_video(env, output, owner, request):
    data = output.path.read_bytes()
    spec = UploadSpec(request_id=request, filename='synthetic-output.mp4', size_bytes=len(data), sha256=sha256(data).hexdigest())
    session = env.uploads.create(spec, principal=owner)
    for offset in range(0, len(data), env.uploads.options.chunk_bytes):
        chunk = data[offset:offset + env.uploads.options.chunk_bytes]
        env.uploads.append(session['upload_id'], offset, chunk, sha256(chunk).hexdigest(), principal=owner)
    return env.uploads.complete(session['upload_id'], principal=owner)


def test_known_variant_reupload_preserves_lineage_without_disclosing_to_other_owner(variants):
    env = variants
    planned = plan(env, recipe=Recipe(mask=MaskSpec(revision='synthetic-mask')))
    output = produced(env, planned); item = register(env, planned, output)
    known = upload_video(env, output, env.owner, 'known')
    assert known['asset_id'] == item.asset_id and known['job_id'] is None
    stranger = Principal(actor_id='synthetic-stranger')
    other = upload_video(env, output, stranger, 'independent')
    assert other['asset_id'] != item.asset_id
    imported = env.catalog.get_asset(other['asset_id'], principal=stranger)
    assert imported.root_asset_id == imported.asset_id and imported.parent_asset_id is None
    with pytest.raises(AccessDenied):
        env.variants.get_plan(planned['plan_id'], principal=stranger)
    with env.database.transaction() as connection:
        receipt = connection.execute('SELECT validation_json FROM uploads WHERE upload_id=?', (other['upload_id'],)).fetchone()[0]
        assert json.loads(receipt)['processing_history'] == 'external_unknown'


def test_nonzero_source_origin_uses_stream_span_not_container_endpoint(variants):
    env = variants
    target = env.originals / 'synthetic-offset.mp4'
    env.variants.tools.ffmpeg_run(['-v', 'error', '-i', str(env.parent_path), '-c', 'copy', '-output_ts_offset', '5', '-y', str(target)], timeout=20)
    parent = env.catalog.register_file(FilesystemBackend(env.originals), target.name, source_backend_id='synthetic-offset',
        principal=env.owner, request_id='offset', name='Synthetic offset source')
    planned = plan(env, parent=parent, recipe=Recipe(speed=2.0))
    clock = planned['contract']['time_map']['clock']
    assert clock['origin_seconds'] == pytest.approx(5.0)
    assert clock['duration_seconds'] == pytest.approx(1.6)


def test_multiple_audio_tracks_need_explicit_selection_and_keep_offsets(variants):
    env = variants
    target = env.originals / 'synthetic-multi.mp4'
    env.variants.tools.ffmpeg_run(['-v', 'error', '-i', str(env.parent_path), '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono',
        '-t', '1.6', '-map', '0:v', '-map', '1:a', '-map', '1:a', '-c:v', 'copy', '-c:a', 'aac', '-y', str(target)], timeout=20)
    parent = env.catalog.register_file(FilesystemBackend(env.originals), target.name, source_backend_id='synthetic-multi',
        principal=env.owner, request_id='multi', name='Synthetic multi-audio source')
    with pytest.raises(ValueError, match='explicit output track'):
        plan(env, parent=parent)
    planned = plan(env, parent=parent, request='selected', recipe=Recipe(audio_stream_index=2))
    assert planned['contract']['selected_audio_stream'] == 2
    dropped = plan(env, parent=parent, request='dropped', recipe=Recipe(audio='drop'))
    assert dropped['contract']['selected_audio_stream'] is None


def test_http_variant_plan_and_permissions(variants):
    from upload.test_api import app, headers
    from router.test_api import TestClient
    env = variants
    with TestClient(app(env)) as client:
        payload = dict(request_id='api-plan', spec=PlanSpec(parent_asset_id=env.parent.asset_id, expected_revision=1).model_dump(mode='json'))
        assert client.post('/variants/plans', json=payload, headers=headers('worker')).status_code == 403
        response = client.post('/variants/plans', json=payload, headers=headers())
        assert response.status_code == 200
        url = '/variants/plans/' + response.json()['plan_id']
        assert client.get(url, headers=headers()).json()['source_sha256'] == env.parent.sha256
        assert client.get(url, headers=headers('other')).status_code == 403
        assert client.post('/variants/register', json={}, headers=headers('worker')).status_code == 404
