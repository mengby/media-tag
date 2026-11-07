from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import sqlite3
import threading

import pytest

from media_tag.catalog import AccessDenied, Principal, RevisionConflict
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from media_tag.storage import IntegrityError, StorageError
from media_tag.upload.models import UploadOptions, UploadSpec
from media_tag.upload.service import UploadService
from media_tag.workers.probe import MediaError
from .conftest import append, spec, upload


def test_resume_after_restart_idempotent_chunks_and_auto_queue(intake):
    env = intake
    session = env.uploads.create(spec(env), principal=env.owner)
    assert env.uploads.create(spec(env), principal=env.owner)['upload_id'] == session['upload_id']
    first, last = env.data[:10], env.data[10:]
    assert append(env, session, first)['offset'] == 10
    assert append(env, session, first)['offset'] == 10
    env.uploads = UploadService(env.router, env.uploads.tools, options=env.uploads.options)
    assert env.uploads.get(session['upload_id'], principal=env.owner)['offset'] == 10
    assert append(env, session, last, 10)['offset'] == len(env.data)
    completed = env.uploads.complete(session['upload_id'], principal=env.owner)
    assert completed['state'] == 'completed' and completed['job_id']
    assert env.uploads.complete(session['upload_id'], principal=env.owner) == completed
    worker = LocalExecutor(env.router, env.workers[0], CPUHandlers(env.router, env.workers[0], env.uploads.tools))
    assert worker.run_once()['accepted'] and worker.run_once()['accepted']
    assert env.router.get_job(completed['job_id'], principal=env.owner)['state'] == 'succeeded'
    asset = env.catalog.get_asset(completed['asset_id'], principal=env.owner)
    assert asset.index_status == 'ai_draft' and asset.sources[0].license_status == 'unverified'
    assert asset.sources[0].source_key.startswith(f"jobs/{session['upload_id']}/")
    with env.catalog.open_content(asset.asset_id, principal=env.owner) as stream:
        assert stream.read() == env.data


def test_duplicate_upload_copies_blob_once_but_separates_sources_and_permissions(intake):
    env = intake
    first = upload(env)
    other = Principal(actor_id='synthetic-other-uploader')
    second = upload(env, request='another', principal=other)
    assert first['asset_id'] != second['asset_id']
    assert len(list(env.objects.glob('objects/*/' + sha256(env.data).hexdigest()))) == 1
    with pytest.raises(AccessDenied):
        env.catalog.get_asset(first['asset_id'], principal=other)
    with pytest.raises(AccessDenied):
        env.uploads.get(first['upload_id'], principal=other)
    assert env.catalog.get_asset(second['asset_id'], principal=other).sources[0].recorded_by == other.actor_id


@pytest.mark.parametrize('problem', ['incomplete', 'offset', 'conflicting_chunk', 'bad_chunk_hash', 'bad_file_hash', 'oversize', 'reused_request'])
def test_invalid_upload_never_publishes(intake, problem):
    env = intake
    model = spec(env)
    if problem == 'bad_file_hash':
        model = UploadSpec.model_validate_json(model.model_dump_json().replace(model.sha256, 'f' * 64))
    session = env.uploads.create(model, principal=env.owner)
    with pytest.raises((ValueError, RevisionConflict, IntegrityError)):
        if problem == 'incomplete':
            env.uploads.complete(session['upload_id'], principal=env.owner)
        elif problem == 'offset':
            append(env, session, offset=1)
        elif problem == 'conflicting_chunk':
            append(env, session, env.data[:10]); append(env, session, b'other data')
        elif problem == 'bad_chunk_hash':
            env.uploads.append(session['upload_id'], 0, env.data, 'a' * 64, principal=env.owner)
        elif problem == 'oversize':
            append(env, session, b'x' * 1025)
        elif problem == 'reused_request':
            env.uploads.create(spec(env, purpose='processing'), principal=env.owner)
        else:
            append(env, session); env.uploads.complete(session['upload_id'], principal=env.owner)
    assert len(env.catalog.snapshot(principal=env.owner)) == 1


@pytest.mark.parametrize('data,filename', [(b'not a png', 'fake.png'), (b'PK\x03\x04fake archive', 'fake.mp4')])
def test_actual_format_or_decode_failure_rejected(intake, data, filename):
    env = intake
    session = env.uploads.create(UploadSpec(request_id='bad', filename=filename, size_bytes=len(data), sha256=sha256(data).hexdigest()), principal=env.owner)
    append(env, session, data)
    with pytest.raises(MediaError):
        env.uploads.complete(session['upload_id'], principal=env.owner)
    assert env.uploads.get(session['upload_id'], principal=env.owner)['state'] == 'rejected'
    with env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM upload_outbox').fetchone()[0] == 0


def test_disguised_valid_media_is_rejected_by_actual_format(intake):
    env = intake
    model = UploadSpec(request_id='mismatch', filename='wrong.mp4', size_bytes=len(env.data), sha256=sha256(env.data).hexdigest())
    session = env.uploads.create(model, principal=env.owner); append(env, session)
    with pytest.raises(MediaError, match='actual format'):
        env.uploads.complete(session['upload_id'], principal=env.owner)


def test_chunk_database_failure_orphan_reuse_and_atomic_parallel_offset(intake):
    env = intake
    session = env.uploads.create(spec(env), principal=env.owner)
    with env.database.transaction(write=True) as connection:
        connection.execute("CREATE TRIGGER synthetic_chunk_failure BEFORE INSERT ON upload_chunks BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):
        append(env, session)
    assert env.uploads.get(session['upload_id'], principal=env.owner)['offset'] == 0
    with env.database.transaction(write=True) as connection:
        connection.execute('DROP TRIGGER synthetic_chunk_failure')
    with ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(lambda _: append(env, session), range(2)))
    assert all(row['offset'] == len(env.data) for row in rows)
    with env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM upload_chunks').fetchone()[0] == 1


def test_finalize_registration_and_outbox_rollback_together(intake):
    env = intake
    session = env.uploads.create(spec(env), principal=env.owner); append(env, session)
    with env.database.transaction(write=True) as connection:
        connection.execute("CREATE TRIGGER synthetic_outbox_failure BEFORE INSERT ON upload_outbox BEGIN SELECT RAISE(ABORT,'synthetic'); END")
    with pytest.raises(sqlite3.IntegrityError):
        env.uploads.complete(session['upload_id'], principal=env.owner)
    assert len(env.catalog.snapshot(principal=env.owner)) == 1
    assert env.uploads.get(session['upload_id'], principal=env.owner)['state'] == 'receiving'
    with env.database.transaction(write=True) as connection:
        connection.execute('DROP TRIGGER synthetic_outbox_failure')
    assert env.uploads.complete(session['upload_id'], principal=env.owner)['state'] == 'completed'


def test_outbox_recovers_crash_after_job_submit_before_ack(intake, monkeypatch):
    env = intake
    session = env.uploads.create(spec(env), principal=env.owner); append(env, session)
    env.uploads.finalize(session['upload_id'], principal=env.owner)
    original = env.router.submit
    def lost_ack(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic process loss')
    monkeypatch.setattr(env.router, 'submit', lost_ack)
    with pytest.raises(RuntimeError):
        env.uploads.dispatch_pending(principal=env.owner)
    monkeypatch.setattr(env.router, 'submit', original)
    env.uploads.dispatch_pending(principal=env.owner)
    with env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM router_jobs').fetchone()[0] == 1
        assert connection.execute('SELECT job_id FROM upload_outbox').fetchone()[0]


@pytest.mark.parametrize('action', ['cancel', 'takeover'])
def test_stale_or_cancelled_finalizer_cannot_register(intake, monkeypatch, action):
    import media_tag.upload.service as module
    env = intake
    session = env.uploads.create(spec(env), principal=env.owner); append(env, session)
    entered, release = threading.Event(), threading.Event()
    original = module.validate_media
    counter = [0]
    def delayed(*args, **kwargs):
        counter[0] += 1
        if counter[0] == 1:
            entered.set(); assert release.wait(8)
        return original(*args, **kwargs)
    monkeypatch.setattr(module, 'validate_media', delayed)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(env.uploads.finalize, session['upload_id'], principal=env.owner)
        try:
            assert entered.wait(5)
            if action == 'cancel':
                env.uploads.cancel(session['upload_id'], principal=env.owner)
            else:
                env.now[0] += 301
                assert env.uploads.complete(session['upload_id'], principal=env.owner)['state'] == 'completed'
        finally:
            release.set()
        with pytest.raises(RevisionConflict):
            future.result(timeout=5)
    assert len(env.catalog.snapshot(principal=env.owner)) == (1 if action == 'cancel' else 2)


def test_quota_worker_forbidden_and_processing_only_queues_probe(intake):
    env = intake
    with pytest.raises(AccessDenied):
        env.uploads.create(spec(env), principal=env.workers[0])
    env.uploads.options = UploadOptions(max_open_per_owner=1, chunk_bytes=1024)
    session = env.uploads.create(spec(env, purpose='processing'), principal=env.owner)
    with pytest.raises(RevisionConflict):
        env.uploads.create(spec(env, request='more'), principal=env.owner)
    append(env, session)
    complete = env.uploads.complete(session['upload_id'], principal=env.owner)
    assert len(env.router.get_job(complete['job_id'], principal=env.owner)['tasks']) == 1


def test_tampered_chunk_and_link_never_enter_catalogue(intake):
    env = intake
    session = env.uploads.create(spec(env), principal=env.owner); append(env, session)
    part = env.workspace / f"jobs/{session['upload_id']}/parts/{sha256(env.data).hexdigest()}"
    part.write_bytes(b'corrupt')
    with pytest.raises(IntegrityError):
        env.uploads.complete(session['upload_id'], principal=env.owner)
    assert len(env.catalog.snapshot(principal=env.owner)) == 1


@pytest.mark.parametrize('filename', ['../secret.png', 'dir/file.png', 'CON.png', 'photo.png:secret', 'photo.png?token'])
def test_client_filename_never_becomes_a_storage_path(filename):
    with pytest.raises(ValueError):
        UploadSpec(request_id='synthetic-name', filename=filename, size_bytes=1, sha256='a' * 64)


def test_actual_staging_junction_or_symlink_cannot_write_outside(intake, tmp_path):
    import os
    from media_tag.workers.evidence import _directory
    from media_tag.storage import UnsafePath
    env = intake
    session = env.uploads.create(spec(env), principal=env.owner)
    folder = _directory(env.workspace, f"jobs/{session['upload_id']}")
    outside = tmp_path / 'outside'; outside.mkdir()
    marker = outside / 'source.txt'; marker.write_bytes(b'read-only synthetic source')
    link = folder / 'parts'
    if os.name == 'nt':
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(UnsafePath):
            append(env, session)
        assert marker.read_bytes() == b'read-only synthetic source' and len(list(outside.iterdir())) == 1
        assert env.uploads.get(session['upload_id'], principal=env.owner)['offset'] == 0
    finally:
        if os.name == 'nt':
            os.rmdir(link)
        else:
            link.unlink()
