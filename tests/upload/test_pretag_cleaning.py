from hashlib import sha256

import pytest

from media_tag.catalog import AccessDenied
from media_tag.cleaning.pretag import current_screen, retry_failed_screen
from media_tag.cleaning.watermark import interpret
from media_tag.contracts.models import Versions
from media_tag.files.service import FileError, FileService
from media_tag.router.models import JobSpec, StepSpec
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from media_tag.storage import IntegrityError
from .conftest import upload


def test_selected_intake_runs_screen_and_verifies_frame(intake, tools):
    env = intake
    asset = upload(env, pretag_cleaning=True)
    router, worker = env.router, env.workers[0]
    router.register_worker(worker.actor_id, tuple(router.stages), principal=env.admin)
    files = FileService(router)
    args = (files, asset["asset_id"], env.owner)
    assert current_screen(*args)["state"] == "queued"
    executor = LocalExecutor(router, worker, CPUHandlers(router,worker,tools))
    for _ in range(3):
        assert executor.run_once()["accepted"]
    assert executor.run_once() is None
    current = current_screen(*args)
    assert current["state"] == "succeeded"
    report = current["report"]
    assert report["source_sha256"] == sha256(env.data).hexdigest()
    assert report["watermark"]["state"] == "awaiting_ocr"
    sample = report["samples"][0]
    raw = current_screen(*args, frame_sha=sample["frame_sha256"], task_id=current["task_id"])
    assert sha256(raw).hexdigest() == sample["frame_sha256"]
    with pytest.raises(FileError):
        current_screen(*args, frame_sha=sample["frame_sha256"], task_id="stale")
    from media_tag.catalog import Principal
    with pytest.raises(AccessDenied):
        current_screen(files, asset["asset_id"], Principal(actor_id="unrelated-reader"))
    normal = upload(env, request="normal-without-screen")
    assert current_screen(files,normal["asset_id"],env.owner) == {"state":"not_requested"}
    # The payload is checked again before the authenticated preview can be read.
    with router.database.transaction() as c:
        import json
        task = json.loads(c.execute("SELECT envelope_json FROM router_attempts WHERE task_id=?",
                                    (current["task_id"],)).fetchone()[0])
    ref = next(r for r in task["input_refs"] if r["sha256"] == sample["frame_sha256"])
    (env.workspace/ref["ref"]).write_bytes(b"damaged")
    with pytest.raises(IntegrityError):
        current_screen(*args, frame_sha=sample["frame_sha256"], task_id=current["task_id"])


def test_normalization_orders_cleaning_and_is_idempotent(intake):
    env=intake
    asset=upload(env,pretag_cleaning=True)
    versions=Versions(rules_version="test",preprocess_version="test")
    def step(name, parents=()):
        return StepSpec(key=name,stage=name,depends_on=parents,versions=versions)
    steps=(step("probe"),step("evidence",("probe",)),step("ocr",("evidence",)),
           step("local_topics",("probe",)),step("visual_fields",("evidence",)))
    with env.database.transaction() as c:
        result=env.router.analysis_steps(c,asset["asset_id"],steps)
        assert env.router.analysis_steps(c,asset["asset_id"],result) == result
    by_key={s.key:s for s in result}
    assert by_key["pretag_cleaning"].depends_on == ("probe","evidence","ocr")
    assert by_key["local_topics"].depends_on == ("probe","pretag_cleaning")
    assert by_key["visual_fields"].depends_on == ("evidence","pretag_cleaning")
    JobSpec(asset_id=asset["asset_id"],steps=result)
    assert interpret("有|右下角|nucleus")["state"] == "candidate_review"
    assert interpret("有|位置|可见字样或图案")["state"] == "uncertain"


def test_scoped_retry_keeps_successful_probe_and_evidence(intake, tools):
    env = intake
    asset = upload(env,pretag_cleaning=True)
    router, worker = env.router, env.workers[0]
    router.register_worker(worker.actor_id,tuple(router.stages),principal=env.admin)
    handler = CPUHandlers(router, worker, tools)
    from media_tag.workers.probe import MediaError
    def fail_screen(lease, stopped):
        if lease.task.stage == "pretag_cleaning":
            raise MediaError("invalid_screen_data","synthetic screening failure")
        return handler(lease,stopped)
    executor = LocalExecutor(router, worker, fail_screen)
    for _ in range(3):
        executor.run_once()
    def tasks():
        return {t["key"]:t for t in router.get_job(asset["job_id"],principal=env.owner)["tasks"]}
    before = tasks()
    assert before["pretag_cleaning"]["state"] in ("failed","needs_attention")
    retry_failed_screen(router, asset["job_id"], env.owner)
    retry_failed_screen(router, asset["job_id"], env.owner)
    after = tasks()
    assert all(before[k]["task_id"]==after[k]["task_id"] for k in ("probe","evidence"))
    assert after["pretag_cleaning"]["task_id"] != before["pretag_cleaning"]["task_id"]
    LocalExecutor(router,worker,handler).run_once()
    assert current_screen(FileService(router), asset["asset_id"], env.owner)["state"]=="succeeded"
