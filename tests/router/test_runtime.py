from hashlib import sha256
import threading
import time

import pytest

from media_tag.api.config import LocalConfig, executor_from_config, load_application
from media_tag.catalog import Principal
from media_tag.precision.pool import image_worker_ids, run_image_workers
from media_tag.contracts.models import WorkerResult
from media_tag.router.models import JobSpec, RouterOptions, StepSpec
from media_tag.router.runtime import CPUHandlers, LocalExecutor, cpu_stages
from media_tag.router.service import Router
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import Toolchain, file_sha256
from .conftest import result, step, submit


def test_preprocessing_can_claim_an_explicit_catalog_registration(env):
    import json
    router = env.router
    lane = env.workers[0]
    submit(env)
    router.worker_intake_scopes[lane.actor_id] = (env.owner.actor_id, '[]')
    router.worker_registration_requests[lane.actor_id] = json.dumps(['different'])
    assert router.claim(principal=lane) is None
    router.worker_registration_requests[lane.actor_id] = json.dumps(['source'])
    router.worker_intake_scopes[lane.actor_id] = ('different-owner', '[]')
    assert router.claim(principal=lane) is None
    router.worker_intake_scopes[lane.actor_id] = (env.owner.actor_id, '[]')
    assert router.claim(principal=lane) is not None


def test_preprocessing_requires_selected_owner_completed_upload_and_free_cpu_slot(env):
    import json
    from media_tag.contracts.models import TaskLimits
    router = env.router
    lane = env.workers[0]
    submit(env, steps=(step('a-gpu', limits=TaskLimits(gpu_slots=1)), step('b-cpu')))
    router.worker_intake_scopes[lane.actor_id] = (env.owner.actor_id, json.dumps(['selected']))
    def claim():
        return router.claim(principal=lane)
    assert claim() is None
    with env.database.transaction(write=True) as db:
        db.execute("""INSERT INTO uploads(upload_id,owner_id,request_id,fingerprint,spec_json,state,asset_id,created_at)
            VALUES('test',?,'selected','test','{}','receiving',?,0)""", (env.owner.actor_id, env.asset.asset_id))
    assert claim() is None
    with env.database.transaction(write=True) as db:
        db.execute("UPDATE uploads SET state='completed'")
    router.worker_intake_scopes[lane.actor_id] = ('different-owner', json.dumps(['selected']))
    assert claim() is None
    router.worker_intake_scopes[lane.actor_id] = (env.owner.actor_id, json.dumps(['other']))
    assert claim() is None
    router.worker_intake_scopes[lane.actor_id] = (env.owner.actor_id, json.dumps(['selected']))
    lease = claim()
    assert lease and lease.task.limits.gpu_slots == 0
    with env.database.transaction() as db:
        assert [r[0] for r in db.execute('SELECT resource FROM router_holds')] == ['cpu:0']


def test_timeout_recovery_builds_capacity_and_backs_off_once_per_cohort():
    from media_tag.precision.pool import RecoveryWindow
    window = RecoveryWindow(8)
    for _ in range(20):
        window.observe(0, 'succeeded', None, 5, 120)
    assert window.limit == 8
    window.observe(0, 'failed', 'precision_timeout', 119, 120)
    window.observe(0, 'failed', 'precision_timeout', 119, 120)
    assert window.limit == 4
    window.observe(1, 'succeeded', None, 80, 120)
    assert window.limit == 2
    for _ in range(2):
        window.observe(2, 'succeeded', None, 8, 120)
    assert window.limit == 4


def test_remote_image_pool_holds_64_leases_and_leaves_cpu_available(env):
    from concurrent.futures import ThreadPoolExecutor

    router = env.router
    ids = image_worker_ids('test-primary', 64)
    full, release, processed, stop = (threading.Event() for _ in range(4))
    lock = threading.Lock()
    started = []

    def handle(lease, stopped):
        with lock:
            started.append(lease.task.task_id)
            if len(started) == 64:
                full.set()
            if len(started) == 65:
                processed.set()
        assert release.wait(45)
        return {'status': 'succeeded', 'artifacts': result(env, lease).artifacts}

    lanes = []
    for identity in ids:
        router.register_worker(identity, ('synthetic',), principal=env.admin)
        router.dedicated_worker_filters[identity] = lambda row: row['step_key'].startswith('image-')
        lanes.append(LocalExecutor(router, Principal(actor_id=identity, role='worker'), handle))
    submit(env, steps=tuple(step(f'image-{i}') for i in range(64)))
    submit(env, steps=(step('image-last'), step('local')), request='overflow')
    with ThreadPoolExecutor(max_workers=1) as scheduler:
        pool = scheduler.submit(run_image_workers, lanes, stop)
        try:
            assert full.wait(35)
            with env.database.transaction() as db:
                assert {row[0] for row in db.execute('SELECT resource FROM router_holds')} == {
                    f'worker:{identity}' for identity in ids}
            local = router.claim(principal=env.workers[0],
                                 prepare_order=lambda rows: [r for r in rows if r['step_key'] == 'local'])
            assert local is not None
            assert not processed.is_set()
            release.set()
            assert processed.wait(35)
        finally:
            release.set()
            stop.set()
            pool.result(timeout=45)
    assert len(set(started)) == 65
    with env.database.transaction() as db:
        assert [r[0] for r in db.execute('SELECT resource FROM router_holds')] == ['cpu:0']


@pytest.fixture(scope="module")
def tools():
    return Toolchain()


def cpu_env(env, tools, kind):
    source = env.originals / ("synthetic.mp4" if kind == "video" else "synthetic.wav")
    if kind == "video":
        tools.ffmpeg_run(["-v", "error", "-f", "lavfi", "-i", "testsrc2=size=80x48:rate=5:duration=1.6",
                          "-c:v", "libx264", "-y", str(source)], timeout=20)
    else:
        tools.ffmpeg_run(["-v", "error", "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono", "-t", "0.5", "-y", str(source)], timeout=20)
    asset = env.catalog.register_file(FilesystemBackend(env.originals), source.name, source_backend_id="synthetic",
                                     principal=env.owner, request_id="cpu-source", name="Synthetic generated media")
    router = Router(env.catalog, env.workspace, cpu_stages(tools))
    router.register_worker(env.workers[0].actor_id, ("probe", "evidence"), principal=env.admin)
    return source, asset, router


@pytest.mark.parametrize("kind", ["video", "audio"])
def test_real_d02_cpu_pipeline_through_queue_is_readonly_and_reuses_success(env, tools, kind):
    source, asset, router = cpu_env(env, tools, kind)
    digest = file_sha256(source)
    versions = step().versions
    steps = (StepSpec(key="probe", stage="probe", versions=versions),
             StepSpec(key="evidence", stage="evidence", depends_on=("probe",), versions=versions))
    spec = JobSpec(asset_id=asset.asset_id, steps=steps)
    job = router.submit(spec, request_id="cpu", principal=env.owner)
    handler = CPUHandlers(router, env.workers[0], tools)
    failures = []
    def checked(lease, stopped):
        try:
            return handler(lease, stopped)
        except Exception as exc:
            failures.append(repr(exc))
            raise
    executor = LocalExecutor(router, env.workers[0], checked)
    assert executor.run_once()["accepted"]
    assert not failures
    assert executor.run_once()["accepted"]
    assert executor.run_once() is None
    status = router.get_job(job["job_id"], principal=env.owner)
    assert status["state"] == "succeeded" and not status["resources"]
    assert file_sha256(source) == digest
    assert router.submit(spec, request_id="reuse", principal=env.owner)["job_id"] == job["job_id"]
    with env.database.transaction() as connection:
        rows = connection.execute("SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=? ORDER BY step_key", (job["job_id"],)).fetchall()
    results = [WorkerResult.model_validate_json(row[0]) for row in rows]
    evidence = next(item for item in results if any(ref.kind == "evidence_manifest" for ref in item.artifacts))
    if kind == "video":
        assert evidence.status == "succeeded" and evidence.evidence
        assert all(item.start_seconds >= 0 for item in evidence.evidence)
        for record in evidence.evidence:
            assert file_sha256(env.workspace / record.artifact.ref) == record.artifact.sha256
    else:
        assert evidence.status == "not_applicable" and evidence.reason == "no_visual_stream"
    assert env.catalog.get_asset(asset.asset_id, principal=env.owner).index_status == "ai_draft"


def test_executor_heartbeats_and_cancel_stops_at_safe_boundary(env):
    # Real wall-clock heartbeat; task remains leased longer than its original lease.
    router = Router(env.catalog, env.workspace, env.router.stages,
                    options=RouterOptions(lease_seconds=0.3, heartbeat_seconds=0.03, backoff_seconds=0.0))
    env.router = router
    job = submit(env)
    entered, exit_handler = threading.Event(), threading.Event()
    observed = []
    def handler(lease, stopped):
        entered.set()
        assert exit_handler.wait(5)
        observed.append(stopped.is_set())
        return {"status": "succeeded", "artifacts": result(env, lease).artifacts}
    executor = LocalExecutor(router, env.workers[0], handler)
    outcomes = []
    thread = threading.Thread(target=lambda: outcomes.append(executor.run_once()))
    thread.start()
    try:
        assert entered.wait(3)
        time.sleep(0.45)
        router.recover(principal=env.admin)
        assert router.get_job(job["job_id"], principal=env.owner)["tasks"][0]["state"] == "running"
        router.cancel(job["job_id"], principal=env.owner)
        time.sleep(0.1)
        assert router.get_job(job["job_id"], principal=env.owner)["resources"]
    finally:
        exit_handler.set()
        thread.join(5)
    assert not thread.is_alive() and observed == [True]
    assert not outcomes[0]["accepted"]
    assert not router.get_job(job["job_id"], principal=env.owner)["resources"]


def test_local_configuration_and_cli_load_private_bindings(env, tools, tmp_path, monkeypatch):
    import media_tag.api.config as module
    monkeypatch.setattr(module, "load_template_bundle", lambda *args, **kwargs: env.contract)
    config = LocalConfig(workspace=str(env.workspace), database=str(env.database.path), object_store=str(env.objects),
                         template_dir=str(env.originals), template_fingerprint=env.contract.fingerprint,
                         credentials={sha256(b"synthetic-private-token").hexdigest(): env.workers[0]},
                         workers=(env.workers[0].actor_id,), tool_dir=str(tools.ffmpeg.parent))
    path = tmp_path / "synthetic.local.json"
    path.write_text(config.model_dump_json())
    app, router, checked_tools = load_application(path)
    assert set(router.stages) == {"probe", "evidence", "pretag_cleaning"} and checked_tools.identity == tools.identity
    assert executor_from_config(path, env.workers[0].actor_id).run_once() is None
    with pytest.raises(ValueError):
        executor_from_config(path, "unregistered")
    from media_tag.cli import main
    assert main(["serve", "--config", str(tmp_path / "missing.local.json")]) == 2
