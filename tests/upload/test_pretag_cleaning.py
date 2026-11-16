from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace

from PIL import Image
import pytest

from media_tag.catalog import AccessDenied, Principal
from media_tag.cleaning.pretag import current_screen, retry_failed_screen
from media_tag.cleaning.watermark import interpret
from media_tag.contracts.models import Versions
from media_tag.files.service import FileError, FileService
from media_tag.router.models import JobSpec, StepSpec
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from media_tag.semantic.runtime import SemanticHandlers
from media_tag.storage import IntegrityError
from .conftest import append, spec, upload


@pytest.mark.parametrize('case', ['shared', 'single_gif', 'other_owner', 'wrong_frame', 'partial'])
def test_watermark_batch_only_adopts_observations_for_bound_images(intake, tools, monkeypatch, case):
    router, worker = intake.router, intake.workers[0]
    registry = SimpleNamespace(revision='watermark-test-v1', root=intake.workspace)
    stage = 'pretag_cleaning'
    router.stages[stage] = replace(router.stages[stage], requires_no_model=False, gpu_allowed=True,
                                   required_gpu_slots=1, required_model_revision=registry.revision)
    router.register_worker(worker.actor_id, tuple(router.stages), principal=intake.admin)
    handler = SemanticHandlers(router, registry)
    calls = []

    def invoke(request, output, task, stopped, **kwargs):
        others = request.get('additional_images', [])
        calls.append(len(others) + 1)
        data = []
        for item in [request, *others]:
            data.append({'frame': item['frame'], 'state': 'candidate_review', 'position': 'corner',
                         'description': item['source_sha256']})
        if others and case == 'wrong_frame':
            data[1] = data[0].copy()
        if others and case == 'partial':
            data.pop()
        return {'ok': True, 'data': data}

    monkeypatch.setattr(handler, 'invoke', invoke)
    owners = [intake.owner, Principal(actor_id='second-owner') if case == 'other_owner' else intake.owner]
    assets = []
    for owner, color in zip(owners, ['green', 'blue']):
        encoded = BytesIO()
        fmt = 'GIF' if case == 'single_gif' and color == 'blue' else 'PNG'
        Image.new('RGB', (32, 24), color).save(encoded, fmt)
        intake.data = encoded.getvalue()
        upload_spec = spec(intake, request=color, pretag_cleaning=True).model_copy(update={'filename': 'image.' + fmt.lower()})
        session = intake.uploads.create(upload_spec, principal=owner)
        append(intake, session, principal=owner)
        assets.append(intake.uploads.complete(session['upload_id'], principal=owner)['asset_id'])
        intake.now[0] += 1
    executor = LocalExecutor(router, worker, CPUHandlers(router, worker, tools, watermark_handler=handler.screen_watermarks),
                             prepare_order=handler.watermark_batch.prepare_order)
    for _ in range(6):
        assert executor.run_once()['accepted']
    assert executor.run_once() is None
    assert calls == {'shared': [2], 'single_gif': [2], 'other_owner': [1, 1], 'wrong_frame': [2, 1], 'partial': [2, 1]}[case]
    for asset, owner in zip(assets, owners):
        report = current_screen(FileService(router), asset, owner)['report']
        assert report['watermark']['visual_observations'] == [{
            'state': 'candidate_review', 'position': 'corner', 'description': report['source_sha256']}]


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
