from hashlib import sha256
import json
from types import SimpleNamespace

from pydantic import Field
import pytest

from media_tag.catalog import Catalog, Database, Principal
from media_tag.contracts.models import ContractModel, Versions, WorkerResult
from media_tag.contracts.templates import FieldDefinition, TemplateBundle
from media_tag.router.models import JobSpec, RouterOptions, StageDefinition, StepSpec
from media_tag.router.service import Router
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.evidence import _directory


class Config(ContractModel):
    batch: int = Field(default=4, ge=1, le=16)


@pytest.fixture
def env(tmp_path):
    originals, objects, workspace = (tmp_path / part for part in ("originals", "objects", "workspace"))
    for path in (originals, objects, workspace):
        path.mkdir()
        (path / "README.md").write_text("Synthetic test only.\n")
    fields = (
        FieldDefinition("note", "文本", "synthetic", "人工", "human_only", None, None),
        FieldDefinition("ai_text", "文本", "synthetic", "AI初稿+人工抽查", "ai_draft_human_sample_review", None, None),
        FieldDefinition("medical", "文本", "synthetic", "AI建议+人工逐条确认", "ai_candidate_per_field_human_review", None, None),
    )
    contract = TemplateBundle("synthetic-v1", "f" * 64, (), (), (("synthetic-template", fields),), True)
    catalog = Catalog(Database(tmp_path / "synthetic.sqlite3"), FilesystemBackend(objects, writable=True), contract=contract)
    owner = Principal(actor_id="synthetic-owner")
    admin = Principal(actor_id="synthetic-admin", role="admin")
    workers = tuple(Principal(actor_id=f"synthetic-worker-{i}", role="worker") for i in range(4))
    now = [1000.0]
    stages = {"synthetic": StageDefinition(Config, "synthetic-v1", gpu_allowed=True, allow_disjoint_rebase=True,
              reduce_config=lambda c: {**c, "batch": max(1, c.get("batch", 4) // 2)}, synthetic=True)}
    options = RouterOptions(lease_seconds=10.0, heartbeat_seconds=2.0, backoff_seconds=2.0)
    router = Router(catalog, workspace, stages, options=options, clock=lambda: now[0])
    for worker in workers:
        router.register_worker(worker.actor_id, ("synthetic",), principal=admin)
    (originals / "synthetic.bin").write_bytes(b"synthetic source bytes")
    asset = catalog.register_file(FilesystemBackend(originals), "synthetic.bin", source_backend_id="synthetic",
                                  principal=owner, request_id="source", name="Synthetic only")
    return SimpleNamespace(router=router, catalog=catalog, database=catalog.database, contract=contract,
                           owner=owner, admin=admin, workers=workers, now=now, asset=asset,
                           originals=originals, objects=objects, workspace=workspace)


def step(key="one", **kwargs):
    return StepSpec(key=key, stage="synthetic", versions=Versions(rules_version="synthetic-v1", preprocess_version="synthetic-v1"), **kwargs)


def submit(env, steps=None, request="request", **kwargs):
    return env.router.submit(JobSpec(asset_id=env.asset.asset_id, steps=steps or (step(),), **kwargs), request_id=request, principal=env.owner)


def result(env, lease, *, status="succeeded", suggestions=None, **kwargs):
    task = lease.task
    data = dict(task_id=task.task_id, attempt=task.attempt, lease_epoch=task.lease_epoch,
                input_fingerprint=task.input_fingerprint, versions=task.versions.model_dump(),
                status=status, elapsed_seconds=0.1)
    if status != "failed":
        output = _directory(env.workspace, task.output_prefix.rstrip("/"))
        (output / "README.md").write_text("Synthetic protocol output; no inference.\n")
        path = output / "synthetic.json"
        path.write_bytes(b'{"synthetic":true}')
        artifact = dict(kind="image", ref=path.relative_to(env.workspace).as_posix(), sha256=sha256(path.read_bytes()).hexdigest())
        data["artifacts"] = [artifact]
        if suggestions:
            data["evidence"] = [dict(evidence_id="synthetic-e", kind="image", source_sha256=task.input_refs[0].sha256, artifact=artifact)]
            data["suggestions"] = suggestions
    data.update(kwargs)
    return WorkerResult.model_validate_json(json.dumps(data))


def finish(env, lease, worker=None, **kwargs):
    worker = worker or env.workers[0]
    outcome = env.router.complete(result(env, lease, **kwargs), principal=worker)
    env.router.release(lease.task.task_id, lease.task.lease_epoch, principal=worker)
    return outcome
