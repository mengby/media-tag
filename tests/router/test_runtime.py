from hashlib import sha256
import threading
import time

import pytest

from media_tag.api.config import LocalConfig, executor_from_config, load_application
from media_tag.contracts.models import WorkerResult
from media_tag.router.models import JobSpec, RouterOptions, StepSpec
from media_tag.router.runtime import CPUHandlers, LocalExecutor, cpu_stages
from media_tag.router.service import Router
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import Toolchain, file_sha256
from .conftest import result, step, submit


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
    assert set(router.stages) == {"probe", "evidence"} and checked_tools.identity == tools.identity
    assert executor_from_config(path, env.workers[0].actor_id).run_once() is None
    with pytest.raises(ValueError):
        executor_from_config(path, "unregistered")
    from media_tag.cli import main
    assert main(["serve", "--config", str(tmp_path / "missing.local.json")]) == 2
