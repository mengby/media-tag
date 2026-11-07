from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from pydantic import ValidationError

from media_tag.catalog import AccessDenied, Principal
from media_tag.contracts.models import EffectiveValue, FieldValue, TaskLimits, WorkerResult
from media_tag.router.models import JobSpec
from media_tag.router.service import Router, RouterConflict
from .conftest import finish, result, step, submit


def view(env, job):
    return env.router.get_job(job["job_id"], principal=env.owner)


@pytest.mark.parametrize("steps", [(step(depends_on=("absent",)),), (step(depends_on=("one",)),),
                                  (step(), step()), (step(depends_on=("two",)), step("two", depends_on=("one",)))])
def test_invalid_dag_is_rejected(env, steps):
    with pytest.raises(ValidationError):
        submit(env, steps)


def test_idempotency_coalescing_and_verified_cache(env):
    job = submit(env)
    assert submit(env)["job_id"] == job["job_id"]
    assert submit(env, request="another")["job_id"] == job["job_id"]
    with pytest.raises(RouterConflict):
        submit(env, (step(config={"batch": 2}),))
    lease = env.router.claim(principal=env.workers[0])
    finish(env, lease)
    assert view(env, job)["state"] == "succeeded"
    assert submit(env, request="cached")["job_id"] == job["job_id"]
    (env.workspace / lease.task.output_prefix / "synthetic.json").write_bytes(b"corrupt")
    assert submit(env, request="changed-artifact")["job_id"] != job["job_id"]


def test_claim_is_atomic_and_cpu_quota_persists_across_instances(env):
    submit(env, (step("a"), step("b"), step("c")))
    with ThreadPoolExecutor(max_workers=4) as pool:
        leases = list(pool.map(lambda w: env.router.claim(principal=w), env.workers))
    claims = [lease for lease in leases if lease]
    assert len(claims) == 2 and len({lease.task.task_id for lease in claims}) == 2
    restarted = Router(env.catalog, env.workspace, env.router.stages, options=env.router.options, clock=lambda: env.now[0])
    assert restarted.claim(principal=env.workers[3]) is None
    with env.database.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM router_holds").fetchone()[0] == 2


def test_dag_waits_for_result_and_resource_release(env):
    job = submit(env, (step("probe"), step("child", depends_on=("probe",)), step("independent")))
    # Deterministic ordering: independent is ready before probe; child is waiting.
    independent = env.router.claim(principal=env.workers[0])
    assert independent.task.stage == "synthetic"
    parent = env.router.claim(principal=env.workers[1])
    assert env.router.complete(result(env, parent), principal=env.workers[1])["accepted"]
    finish(env, independent)
    assert env.router.claim(principal=env.workers[0]) is None
    env.router.release(parent.task.task_id, parent.task.lease_epoch, principal=env.workers[1])
    child = env.router.claim(principal=env.workers[0])
    assert len(child.task.input_refs) == 2
    finish(env, child)
    assert view(env, job)["state"] == "succeeded"


def test_heartbeat_expiry_fencing_and_conservative_gpu_recovery(env):
    job = submit(env, (step(limits=TaskLimits(gpu_slots=1)),))
    lease = env.router.claim(principal=env.workers[0])
    env.now[0] += 8
    assert env.router.heartbeat(lease.task.task_id, 1, principal=env.workers[0])["alive"]
    env.now[0] += 9
    assert view(env, job)["tasks"][0]["state"] == "running"
    env.now[0] += 2
    env.router.recover(principal=env.admin)
    assert not env.router.complete(result(env, lease), principal=env.workers[0])["accepted"]
    env.now[0] += 3
    assert env.router.claim(principal=env.workers[1]) is None
    assert all(item["quarantined"] for item in view(env, job)["resources"])
    env.router.process_stopped = lambda task, epoch: False
    with pytest.raises(RouterConflict):
        env.router.confirm_stopped(lease.task.task_id, 1, principal=env.admin)
    env.router.release(lease.task.task_id, 1, principal=env.workers[0])
    newer = env.router.claim(principal=env.workers[1])
    assert newer.task.attempt == 2 and newer.task.lease_epoch == 2
    assert not env.router.complete(result(env, lease), principal=env.workers[0])["accepted"]
    assert not env.router.release(lease.task.task_id, 1, principal=env.workers[0])
    assert len(view(env, job)["resources"]) == 2
    finish(env, newer, env.workers[1])


def test_single_gpu_across_independent_stage_types(env):
    env.router.stages["other"] = env.router.stages["synthetic"]
    env.router.register_worker(env.workers[1].actor_id, ("other",), principal=env.admin)
    other = json.loads(step("b", limits=TaskLimits(gpu_slots=1)).model_dump_json())
    other["stage"] = "other"
    from media_tag.router.models import StepSpec
    submit(env, (step("a", limits=TaskLimits(gpu_slots=1)), StepSpec.model_validate_json(json.dumps(other))))
    lease = env.router.claim(principal=env.workers[0])
    assert env.router.claim(principal=env.workers[1]) is None
    finish(env, lease)
    assert env.router.claim(principal=env.workers[1]) is not None


def test_duplicate_results_once_and_conflicts_audited_without_worker_logs(env):
    job = submit(env)
    lease = env.router.claim(principal=env.workers[0])
    value = result(env, lease)
    assert env.router.complete(value, principal=env.workers[0])["accepted"]
    assert env.router.complete(value, principal=env.workers[0])["duplicate"]
    changed = json.loads(value.model_dump_json())
    changed["elapsed_seconds"] = 0.2
    assert env.router.complete(WorkerResult.model_validate_json(json.dumps(changed)), principal=env.workers[0])["reason"] == "result_conflict"
    with env.database.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM router_events WHERE action='result'").fetchone()[0] == 1
        assert [row[0] for row in connection.execute("SELECT reason FROM router_receipts ORDER BY receipt_id")] == ["accepted", "duplicate", "result_conflict"]
    env.router.release(lease.task.task_id, 1, principal=env.workers[0])
    assert view(env, job)["state"] == "succeeded"


@pytest.mark.parametrize("corrupt", ["hash", "missing", "wrong_fingerprint", "foreign_output", "lease_expired_during_validation"])
def test_invalid_artifacts_or_binding_never_complete(env, corrupt, monkeypatch):
    job = submit(env)
    lease = env.router.claim(principal=env.workers[0])
    value = result(env, lease)
    data = json.loads(value.model_dump_json())
    if corrupt == "hash":
        (env.workspace / data["artifacts"][0]["ref"]).write_bytes(b"different")
    elif corrupt == "missing":
        (env.workspace / data["artifacts"][0]["ref"]).unlink()
    elif corrupt == "wrong_fingerprint":
        data["input_fingerprint"] = "e" * 64
    elif corrupt == "foreign_output":
        data["artifacts"][0] = lease.task.input_refs[0].model_dump()
    else:
        original = env.router._artifacts
        def slow_check(task, result):
            original(task, result)
            env.now[0] += 11
        monkeypatch.setattr(env.router, "_artifacts", slow_check)
    outcome = env.router.complete(WorkerResult.model_validate_json(json.dumps(data)), principal=env.workers[0])
    assert not outcome["accepted"]
    assert view(env, job)["tasks"][0]["state"] == "needs_attention"


def test_retry_backoff_budget_and_nonretryable_errors(env):
    job = submit(env)
    for attempt in range(1, 4):
        lease = env.router.claim(principal=env.workers[0])
        assert lease.task.attempt == attempt
        finish(env, lease, status="failed", errors=[dict(code="transient", message="secret worker log", retryable=True)])
        assert env.router.claim(principal=env.workers[0]) is None
        env.now[0] += 2 ** attempt
    assert view(env, job)["state"] == "failed"
    with env.database.transaction() as connection:
        assert "secret worker log" not in connection.execute("SELECT group_concat(result_json) FROM router_attempts").fetchone()[0]
    job2 = submit(env, request="permanent", max_total_attempts=1)
    lease = env.router.claim(principal=env.workers[0])
    finish(env, lease, status="failed", errors=[dict(code="auth_failed", message="private", retryable=True)])
    env.now[0] += 100
    assert view(env, job2)["state"] == "needs_attention"


def test_oom_reduces_once_and_changes_fingerprint(env):
    job = submit(env, (step(config={"batch": 4}),))
    first = env.router.claim(principal=env.workers[0])
    finish(env, first, status="failed", errors=[dict(code="oom", message="synthetic OOM", retryable=True)])
    env.now[0] += 2
    second = env.router.claim(principal=env.workers[0])
    assert second.config == {"batch": 2} and second.task.input_fingerprint != first.task.input_fingerprint
    assert second.task.versions == first.task.versions
    finish(env, second, status="failed", errors=[dict(code="oom", message="synthetic second OOM", retryable=True)])
    env.now[0] += 10
    assert env.router.claim(principal=env.workers[0]) is None
    assert view(env, job)["state"] == "needs_attention"


def test_job_attempt_budget_and_total_timeout_with_healthy_heartbeats(env):
    job = submit(env, (step(limits=TaskLimits(timeout_seconds=12)),), max_total_attempts=1)
    lease = env.router.claim(principal=env.workers[0])
    for _ in range(2):
        env.now[0] += 5
        assert env.router.heartbeat(lease.task.task_id, 1, principal=env.workers[0])["alive"]
    env.now[0] += 3
    assert not env.router.heartbeat(lease.task.task_id, 1, principal=env.workers[0])["alive"]
    env.router.release(lease.task.task_id, 1, principal=env.workers[0])
    env.now[0] += 2
    assert env.router.claim(principal=env.workers[0]) is None
    assert view(env, job)["tasks"][0]["error_code"] == "attempt_budget"


def test_pause_resume_cancel_and_owner_permissions(env):
    job = submit(env, (step("a"), step("b")))
    lease = env.router.claim(principal=env.workers[0])
    env.router.pause(job["job_id"], principal=env.owner)
    assert env.router.claim(principal=env.workers[1]) is None
    finish(env, lease)
    env.router.pause(job["job_id"], principal=env.owner, paused=False)
    env.router.pause_dispatch(principal=env.admin)
    assert env.router.claim(principal=env.workers[0]) is None
    env.router.pause_dispatch(principal=env.admin, paused=False)
    next_lease = env.router.claim(principal=env.workers[0])
    env.router.cancel(job["job_id"], principal=env.owner)
    assert not env.router.heartbeat(next_lease.task.task_id, 1, principal=env.workers[0])["alive"]
    assert not env.router.complete(result(env, next_lease), principal=env.workers[0])["accepted"]
    assert view(env, job)["state"] == "cancelled" and view(env, job)["resources"]
    for action in (lambda: env.router.get_job(job["job_id"], principal=env.workers[0]),
                   lambda: env.router.pause(job["job_id"], principal=Principal(actor_id="stranger")),
                   lambda: env.router.pause_dispatch(principal=env.owner)):
        with pytest.raises(AccessDenied):
            action()


def test_replan_preserves_unaffected_tasks_and_fences_running_generation(env):
    job = submit(env, (step("a"), step("b", depends_on=("a",)), step("c", depends_on=("b",))))
    first = env.router.claim(principal=env.workers[0])
    finish(env, first)
    second = env.router.claim(principal=env.workers[0])
    revised = env.router.replan(job["job_id"], (step("b", config={"batch": 2}, depends_on=("a",)),), expected_revision=1, principal=env.owner)
    by_key = {t["key"]: t for t in revised["tasks"]}
    assert by_key["a"]["task_id"] == first.task.task_id and by_key["a"]["state"] == "succeeded"
    assert by_key["b"]["task_id"] != second.task.task_id and by_key["c"]["attempt"] == 0
    assert not env.router.complete(result(env, second), principal=env.workers[0])["accepted"]
    with pytest.raises(RouterConflict):
        env.router.replan(job["job_id"], (step("b"),), expected_revision=1, principal=env.owner)
    assert submit(env, request="original-plan")["job_id"] != job["job_id"]


def human(field, revision):
    return EffectiveValue(field=field, value=FieldValue(state="observed", values=("human value",)), actor_id="synthetic-owner",
                          revision=revision, evidence_ids=("synthetic-human-record",), confirmed_at=datetime.now(timezone.utc))


def suggestion(field="ai_text"):
    return dict(suggestion_id="synthetic-suggestion", field=field, value=dict(state="observed", values=["synthetic suggestion"]),
                origin="ai", producer_version="synthetic-v1", evidence_ids=["synthetic-e"])


def test_disjoint_rebase_and_atomic_suggestions_do_not_overwrite_human(env):
    submit(env, (step(target_fields=("ai_text",)),))
    lease = env.router.claim(principal=env.workers[0])
    env.catalog.revise(env.asset.asset_id, principal=env.owner, expected_revision=1, reason="synthetic edit", values=(human("note", 2),))
    assert finish(env, lease, suggestions=[suggestion()])["accepted"]
    asset = env.catalog.get_asset(env.asset.asset_id, principal=env.owner)
    assert asset.revision == 2 and asset.effective_values[0].value.values == ("human value",)
    assert len(asset.suggestions) == 1
    with pytest.raises(AccessDenied):
        env.catalog.get_asset(env.asset.asset_id, principal=env.workers[0])
    with env.database.transaction() as connection:
        assert connection.execute("SELECT adopted_revision FROM router_attempts").fetchone()[0] == 2


@pytest.mark.parametrize("field", ["ai_text", "medical"])
def test_target_human_change_or_medical_observation_cannot_be_adopted(env, field):
    submit(env, (step(target_fields=(field,)),))
    lease = env.router.claim(principal=env.workers[0])
    if field == "ai_text":
        env.catalog.revise(env.asset.asset_id, principal=env.owner, expected_revision=1, reason="synthetic edit", values=(human(field, 2),))
    assert not finish(env, lease, suggestions=[suggestion(field)])["accepted"]
    assert not env.catalog.get_asset(env.asset.asset_id, principal=env.owner).suggestions


def test_adoption_and_queue_ack_rollback_together_on_database_failure(env):
    job = submit(env, (step(target_fields=("ai_text",)),))
    lease = env.router.claim(principal=env.workers[0])
    value = result(env, lease, suggestions=[suggestion()])
    with env.database.transaction(write=True) as connection:
        connection.execute("CREATE TRIGGER synthetic_failure BEFORE INSERT ON router_receipts BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        env.router.complete(value, principal=env.workers[0])
    assert not env.catalog.get_asset(env.asset.asset_id, principal=env.owner).suggestions
    assert view(env, job)["tasks"][0]["state"] == "leased"
    with env.database.transaction(write=True) as connection:
        connection.execute("DROP TRIGGER synthetic_failure")
    assert env.router.complete(value, principal=env.workers[0])["accepted"]


def test_revoked_task_owner_read_access_stops_worker_without_broad_grants(env):
    delegate = Principal(actor_id="synthetic-delegate")
    env.catalog.change_access(env.asset.asset_id, principal=env.owner, expected_revision=1, subject_id=delegate.actor_id, permission="read", reason="synthetic grant")
    job = env.router.submit(JobSpec(asset_id=env.asset.asset_id, steps=(step(),)), request_id="delegate", principal=delegate)
    lease = env.router.claim(principal=env.workers[0])
    env.catalog.change_access(env.asset.asset_id, principal=env.owner, expected_revision=2, subject_id=delegate.actor_id, permission="read", reason="synthetic revoke", revoke=True)
    assert not env.router.heartbeat(lease.task.task_id, 1, principal=env.workers[0])["alive"]
    assert not env.router.complete(result(env, lease), principal=env.workers[0])["accepted"]
    with pytest.raises(RouterConflict):
        with env.router.open_input(lease.task.task_id, 1, principal=env.workers[0]):
            pass


def test_read_only_grant_cannot_request_main_catalogue_suggestions(env):
    reader = Principal(actor_id="synthetic-reader")
    env.catalog.change_access(env.asset.asset_id, principal=env.owner, expected_revision=1, subject_id=reader.actor_id, permission="read", reason="synthetic read grant")
    with pytest.raises(AccessDenied):
        env.router.submit(JobSpec(asset_id=env.asset.asset_id, steps=(step(target_fields=("ai_text",)),)), request_id="reader", principal=reader)
    job = env.router.submit(JobSpec(asset_id=env.asset.asset_id, steps=(step(),)), request_id="reader-cpu", principal=reader)
    with pytest.raises(AccessDenied):
        env.router.replan(job["job_id"], (step(target_fields=("ai_text",)),), expected_revision=1, principal=reader)


def test_failed_branch_does_not_block_independent_video_processing(env):
    job = submit(env, (step("semantic"), step("video_process"), step("semantic_child", depends_on=("semantic",))))
    lease = env.router.claim(principal=env.workers[0])
    finish(env, lease, status="failed", errors=[dict(code="unpublished_vocabulary", message="synthetic missing vocabulary")])
    processing = env.router.claim(principal=env.workers[0])
    assert processing and processing.task.task_id == next(t["task_id"] for t in job["tasks"] if t["key"] == "video_process")
    finish(env, processing)
    assert {t["key"]: t["state"] for t in view(env, job)["tasks"]} == {"semantic": "needs_attention", "semantic_child": "needs_attention", "video_process": "succeeded"}


def test_aging_allows_old_background_task_before_new_interactive(env):
    old = submit(env)
    env.now[0] += 31
    interactive = submit(env, (step(config={"batch": 2}),), request="interactive", priority=1)
    first = env.router.claim(principal=env.workers[0])
    assert first.task.job_id == old["job_id"]


def test_server_stage_policy_and_worker_capabilities_cannot_be_spoofed(env):
    from media_tag.router.models import StepSpec
    payload = json.loads(step().model_dump_json())
    payload["stage"] = "unregistered"
    with pytest.raises(ValueError):
        submit(env, (StepSpec.model_validate_json(json.dumps(payload)),))
    with pytest.raises(ValueError):
        submit(env, (step(config={"secret": "forbidden extra config"}),))
    with pytest.raises(ValueError):
        submit(env, (step(limits=TaskLimits(max_attempts=4)),))
    env.router.register_worker(env.workers[0].actor_id, ("synthetic",), enabled=False, principal=env.admin)
    submit(env)
    with pytest.raises(AccessDenied):
        env.router.claim(principal=env.workers[0])
