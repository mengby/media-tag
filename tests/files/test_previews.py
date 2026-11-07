from hashlib import sha256
import json

import pytest

from media_tag.catalog import Principal
from media_tag.files.service import FileService
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.variants.models import Recipe, MaskSpec
from router.test_api import TestClient
from upload.conftest import upload
from upload.test_api import running
from variants.conftest import plan, produced, register
from .conftest import app, content, headers


def analyze(env, asset):
    from media_tag.router.models import JobSpec, StepSpec
    from media_tag.contracts.models import Versions
    versions = Versions(rules_version='synthetic-v1', preprocess_version='d02-cpu-v1')
    job = env.router.submit(JobSpec(asset_id=asset.asset_id, steps=(StepSpec(key='probe', stage='probe', versions=versions),
        StepSpec(key='evidence', stage='evidence', depends_on=('probe',), versions=versions))), request_id='files-' + asset.asset_id, principal=env.owner)
    env.router.register_worker(env.workers[0].actor_id, ('probe', 'evidence'), principal=env.admin)
    worker = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], env.uploads.tools))
    assert worker.run_once()['accepted'] and worker.run_once()['accepted']
    return job


def test_real_png_thumbnail_exact_hash_and_revocation(intake):
    env = intake
    record = upload(env)
    worker = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], env.uploads.tools))
    assert worker.run_once()['accepted'] and worker.run_once()['accepted']
    asset = env.catalog.get_asset(record['asset_id'], principal=env.owner)
    with TestClient(app(env)) as client:
        response = client.get(content(asset, 'preview-manifest'), headers=headers())
        assert response.status_code == 200, response.text
        manifest = response.json()
        assert manifest['state'] == 'ready' and not manifest['full_video_observed']
        thumb = next(item for item in manifest['resources'] if item['kind'] == 'thumbnail')
        fetched = client.get(thumb['content'], headers=headers())
        assert fetched.status_code == 200 and sha256(fetched.content).hexdigest() == thumb['sha256']
        assert fetched.headers['content-type'] == 'image/png' and fetched.headers['content-disposition'].startswith('inline;')
        assert 'jobs/' not in response.text and str(env.workspace) not in response.text
        wrong = thumb['content'].replace(thumb['sha256'], 'f'*64)
        assert client.get(wrong, headers=headers()).status_code == 412
        env.catalog.archive(asset.asset_id, principal=env.owner, expected_revision=1, reason='synthetic withdrawal')
        assert client.get(thumb['content'], headers=headers()).status_code == 404


@pytest.mark.parametrize('failure', ['corrupt', 'missing', 'producer_cancelled'])
def test_invalid_thumbnail_or_producer_is_never_served(intake, failure):
    env = intake; record = upload(env)
    worker = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], env.uploads.tools))
    worker.run_once(); worker.run_once()
    asset = env.catalog.get_asset(record['asset_id'], principal=env.owner)
    with TestClient(app(env)) as client:
        manifest = client.get(content(asset, 'preview-manifest'), headers=headers()).json()
        thumb = next(item for item in manifest['resources'] if item['kind'] == 'thumbnail')
        if failure == 'producer_cancelled':
            env.router.cancel(record['job_id'], principal=env.owner)
        else:
            with env.database.transaction() as connection:
                rows = connection.execute('SELECT result_json FROM router_attempts WHERE result_json IS NOT NULL').fetchall()
            ref = next(e['artifact']['ref'] for row in rows for e in json.loads(row[0])['evidence'])
            path = env.workspace/ref
            if failure == 'missing': path.unlink()
            else: path.write_bytes(b'changed')
        result = client.get(thumb['content'], headers=headers(**{'If-None-Match': '*'}))
        assert result.status_code == (404 if failure == 'producer_cancelled' else 503)
        assert 'changed' not in result.text or result.json()['error']['code'] == 'preview_content_changed'


def test_nonmasked_variant_files_are_independently_authorized_and_keep_time_map(variants):
    env = variants; planned = plan(env, recipe=Recipe(speed=2.0)); output = produced(env, planned); asset = register(env, planned, output)
    other = Principal(actor_id='synthetic-file-other', role='reader')
    env.catalog.change_access(asset.asset_id, principal=env.owner, expected_revision=1, subject_id=other.actor_id, permission='read', reason='synthetic version grant')
    with TestClient(app(env)) as client:
        response = client.get(content(asset, 'download-manifest'), headers=headers('other'))
        assert response.status_code == 200, response.text
        manifest = response.json()
        assert manifest['transform']['cumulative_speed'] == 2 and not manifest['transform']['apply_transform_again']
        assert manifest['technical']['time_basis']['duration_seconds'] == pytest.approx(0.8)
        result = client.get(manifest['content'], headers=headers('other'))
        assert result.content == output.path.read_bytes() and sha256(result.content).hexdigest() == asset.sha256
        details = client.get(f'/v1/assets/{asset.asset_id}', headers=headers('other')).json()
        assert [v['asset_id'] for v in details['versions']] == [asset.asset_id]
        assert client.get(content(env.parent), headers=headers('other')).status_code == 403
        assert client.get(content(asset, 'preview-content'), headers=headers('other')).content == output.path.read_bytes()


def test_pending_privacy_output_and_its_unmasked_child_are_blocked_for_every_role(variants):
    env = variants
    first = plan(env, recipe=Recipe(mask=MaskSpec(revision='synthetic-mask')))
    parent = register(env, first, produced(env, first))
    child_plan = plan(env, request='child', parent=parent, chain=True, recipe=Recipe(speed=2.0))
    child = register(env, child_plan, produced(env, child_plan))
    with TestClient(app(env)) as client:
        for asset in (parent, child):
            for role in ('reader', 'admin'):
                for operation in ('download-manifest', 'content', 'preview-manifest', 'preview-content'):
                    response = client.get(content(asset, operation), headers=headers(role))
                    assert response.status_code == 409 and response.json()['error']['code'] == 'privacy_review_pending'
        assert client.get(content(env.parent), headers=headers()).status_code == 200


@pytest.mark.parametrize('failure', ['missing', 'producer_cancelled'])
def test_variant_file_missing_never_replaces_it_with_original(variants, failure):
    env = variants; planned = plan(env); output = produced(env, planned); asset = register(env, planned, output)
    # A 1x synthetic output may have identical bytes to the root; remove only the selected immutable object's location.
    if failure == 'missing':
        with env.database.transaction(write=True) as connection:
            connection.execute('DELETE FROM storage_locations WHERE blob_sha256=?', (asset.sha256,))
    else:
        env.router.cancel(output.job['job_id'], principal=env.owner)
    with TestClient(app(env)) as client:
        assert client.get(content(asset), headers=headers()).status_code == (503 if failure == 'missing' else 409)
    assert env.parent_path.exists()


def test_video_preview_native_range_and_actual_pts_thumbnails(variants):
    env = variants; analyze(env, env.parent)
    with TestClient(app(env)) as client:
        manifest = client.get(content(env.parent, 'preview-manifest'), headers=headers()).json()
        native = next(item for item in manifest['resources'] if item['kind'] == 'source_media')
        thumbs = [item for item in manifest['resources'] if item['kind'] == 'thumbnail']
        assert native['time_map']['kind'] == 'identity' and native['whole_file']
        assert native['time_basis']['duration_seconds'] == pytest.approx(1.6)
        assert thumbs and all(t['source_seconds'] >= 0 for t in thumbs)
        response = client.get(native['content'], headers=headers(Range='bytes=0-127'))
        assert response.status_code == 206 and response.content == env.parent_path.read_bytes()[:128]
        assert client.get(f'/v1/assets/{env.parent.asset_id}', headers=headers()).json()['technical']['kind'] == 'video'


def test_optional_analysis_failure_does_not_block_verified_download(variants):
    env = variants; analyze(env, env.parent)
    with env.database.transaction() as connection:
        rows = connection.execute('SELECT result_json FROM router_attempts WHERE result_json IS NOT NULL').fetchall()
    ref = next(a['ref'] for row in rows for a in json.loads(row[0])['artifacts'] if a['kind'] == 'technical_report')
    (env.workspace/ref).unlink()
    with TestClient(app(env)) as client:
        manifest = client.get(content(env.parent, 'download-manifest'), headers=headers()).json()
        assert manifest['technical']['report_state'] == 'unavailable'
        assert client.get(manifest['content'], headers=headers()).content == env.parent_path.read_bytes()


def test_real_loopback_download_resume_and_decode(variants, tmp_path):
    env = variants
    with running(app(env)) as client:
        manifest = client.get(content(env.parent, 'download-manifest'), headers=headers()).json()
        first = client.get(manifest['content'], headers=headers(Range='bytes=0-127'))
        second = client.get(manifest['content'], headers=headers(**{'Range': 'bytes=128-', 'If-Range': manifest['etag']}))
        assert first.status_code == second.status_code == 206
        combined = first.content + second.content
        assert sha256(combined).hexdigest() == manifest['sha256']
        target = tmp_path/'downloaded.mp4'; target.write_bytes(combined)
        env.variants.tools.ffmpeg_run(['-v', 'error', '-xerror', '-i', str(target), '-f', 'null', '-'], timeout=20)
        assert client.request('HEAD', manifest['content'], headers=headers()).headers['content-length'] == str(len(combined))


@pytest.mark.parametrize('status', ['pending', 'cancelled', 'failed'])
def test_thumbnail_lifecycle_does_not_masquerade_as_missing_or_success(intake, status):
    env = intake; record = upload(env)
    if status == 'cancelled':
        env.router.cancel(record['job_id'], principal=env.owner)
    elif status == 'failed':
        # Synthetic scheduler fault injection, not a claim that a model ran.
        with env.database.transaction(write=True) as connection:
            connection.execute("UPDATE router_tasks SET state='failed',error_code='synthetic_failure' WHERE job_id=?", (record['job_id'],))
    asset = env.catalog.get_asset(record['asset_id'], principal=env.owner)
    with TestClient(app(env)) as client:
        manifest = client.get(content(asset, 'preview-manifest'), headers=headers()).json()
        assert manifest['state'] == 'ready' and manifest['thumbnail_state'] == status
        assert len(manifest['resources']) == 1  # Authorized native PNG remains independent of analysis.


def test_audio_visual_evidence_is_not_applicable_without_blocking_audio_preview(intake):
    env = intake
    target = env.originals/'synthetic-audio.wav'
    env.uploads.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'anullsrc=r=8000:cl=mono', '-t', '0.5', '-y', str(target)], timeout=20)
    asset = env.catalog.register_file(FilesystemBackend(env.originals), target.name, source_backend_id='synthetic-audio', principal=env.owner, request_id='audio', name='Synthetic audio')
    analyze(env, asset)
    with TestClient(app(env)) as client:
        manifest = client.get(content(asset, 'preview-manifest'), headers=headers()).json()
        assert manifest['state'] == 'ready' and manifest['thumbnail_state'] == 'not_applicable'
        assert all(r['kind'] != 'thumbnail' for r in manifest['resources'])
