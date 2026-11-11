"""Single SQLite transaction path for claims, fencing, resource holds and result adoption."""

from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Callable, Iterator
from uuid import uuid4

from media_tag.catalog import AccessDenied, Catalog, NotFound, Principal, RevisionConflict
from media_tag.catalog.database import UnsupportedRevision
from media_tag.catalog.service import encode
from media_tag.contracts.models import ArtifactRef, TaskEnvelope, WorkerResult, validate_result_for_task
from media_tag.storage import StorageError
from media_tag.storage.filesystem import FilesystemBackend
from .models import JobSpec, Lease, RouterOptions, StageDefinition, StepSpec

TERMINAL = {"succeeded", "failed", "needs_attention", "cancelled"}


class RouterConflict(RevisionConflict):
    pass


def _digest(value) -> str:
    return sha256(encode(value).encode()).hexdigest()


class Router:
    def __init__(self, catalog: Catalog, workspace: Path, stages: dict[str, StageDefinition], *,
                 options: RouterOptions | None = None, clock: Callable[[], float] = time.time,
                 process_stopped: Callable[[str, int], bool] | None = None) -> None:
        self.catalog, self.database = catalog, catalog.database
        self.workspace = Path(workspace).resolve()
        self.files = FilesystemBackend(self.workspace)
        self.stages = dict(stages)
        self.options = options or RouterOptions()
        self.clock = clock
        self.process_stopped = process_stopped

    def _validate_steps(self, steps):
        for step in steps:
            definition = self.stages.get(step.stage)
            if definition is None:
                raise ValueError("stage is not registered on the server")
            definition.config_model.model_validate_json(encode(step.config))
            if definition.requires_no_model and step.versions.model_revision is not None:
                raise ValueError('this deterministic stage has no model dependency')
            if definition.required_model_revision is not None and step.versions.model_revision != definition.required_model_revision:
                raise ValueError('model revision does not match the server registry')
            if definition.required_preprocess_version is not None and step.versions.preprocess_version != definition.required_preprocess_version:
                raise ValueError('preprocess version does not match the registered stage')
            if definition.required_gpu_slots is not None and step.limits.gpu_slots != definition.required_gpu_slots:
                raise ValueError('resource request does not match the deployed model device')
            if definition.allowed_target_fields is not None and not set(step.target_fields) <= set(definition.allowed_target_fields):
                raise ValueError('model evidence stage cannot write the requested catalogue fields')
            if step.limits.max_attempts > self.options.max_attempts or step.limits.timeout_seconds > self.options.max_timeout_seconds:
                raise ValueError("task limits exceed server policy")
            if type(step.limits.gpu_slots) is not int or (step.limits.gpu_slots and not definition.gpu_allowed):
                raise ValueError("stage is not permitted to request GPU resources")
            if len(set(step.target_fields)) != len(step.target_fields) or any(field not in self.catalog.fields for field in step.target_fields):
                raise ValueError("unknown or duplicate target field")
            for value in (*step.versions.model_dump().values(), step.prompt_version):
                if value is not None and (len(value) > 128 or any(mark in value for mark in ("://", "?", "=", "\n", "\r"))):
                    raise ValueError("versions must be stable identifiers, not URLs or credentials")

    @staticmethod
    def _job_auth(connection, job_id, principal):
        row = connection.execute("SELECT * FROM router_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("job is unavailable")
        if principal.role not in ("editor", "admin") or (row["owner_id"] != principal.actor_id and principal.role != "admin"):
            raise AccessDenied("job permission denied")
        return row

    def _source(self, connection, job):
        # Delegation is limited to the creator's explicit object read scope, never an inherited global admin role.
        return self.catalog._authorize(connection, job["asset_id"], Principal(actor_id=job["owner_id"]), "read")

    def _task_source(self, connection, job, step):
        source = self.catalog._authorize(connection, job["asset_id"], Principal(actor_id=job["owner_id"]),
                                        "edit" if step.target_fields else "read")
        if source['kind']=='derived' and step.stage in ('probe','evidence','ocr','face','clip'):
            from media_tag.files.service import FileService
            if FileService(self)._gate(connection, source):
                raise AccessDenied('background analysis requires a currently approved, available output')
        return source

    def _event(self, connection, job_id, task_id, actor, action, detail):
        connection.execute("INSERT INTO router_events(job_id,task_id,actor_id,action,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                           (job_id, task_id, actor, action, encode(detail), self.clock()))

    def register_worker(self, worker_id: str, stages: tuple[str, ...], *, principal: Principal, enabled=True):
        if principal.role != "admin":
            raise AccessDenied("administrator capability required")
        Principal(actor_id=worker_id, role="worker")
        if not stages or any(stage not in self.stages for stage in stages):
            raise ValueError("worker stages must be registered server stages")
        with self.database.transaction(write=True) as connection:
            connection.execute("""INSERT INTO router_workers VALUES(?,?,?) ON CONFLICT(worker_id)
                DO UPDATE SET stages_json=excluded.stages_json,enabled=excluded.enabled""", (worker_id, encode(sorted(set(stages))), int(enabled)))

    @staticmethod
    def _worker(connection, principal, *, enabled=True):
        if principal.role != "worker":
            raise AccessDenied("worker identity required")
        worker = connection.execute("SELECT * FROM router_workers WHERE worker_id=?", (principal.actor_id,)).fetchone()
        if worker is None or (enabled and not worker["enabled"]):
            raise AccessDenied("worker is not enabled on this server")
        return worker

    def _new_task(self, connection, job_id, step, generation=1):
        task_id = f"task-{uuid4().hex}"
        connection.execute("INSERT INTO router_tasks(task_id,job_id,step_key,generation,spec_json,state,ready_at) VALUES(?,?,?,?,?,'queued',?)",
                           (task_id, job_id, step.key, generation, step.model_dump_json(), self.clock()))
        return task_id

    def submit(self, spec: JobSpec, *, request_id: str, principal: Principal) -> dict:
        if principal.role not in ("editor", "admin"):
            raise AccessDenied("human job owner required")
        Principal(actor_id=request_id)
        self._validate_steps(spec.steps)
        with self.database.transaction(write=True) as connection:
            asset = self.catalog._authorize(connection, spec.asset_id, Principal(actor_id=principal.actor_id),
                                             "edit" if any(step.target_fields for step in spec.steps) else "read")
            fingerprint = _digest({"spec": spec.model_dump(mode="json"), "source_sha256": asset["blob_sha256"], "revision": asset["revision"]})
            previous = connection.execute("SELECT * FROM router_requests WHERE owner_id=? AND request_id=?", (principal.actor_id, request_id)).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise RouterConflict("request ID was reused with different input")
                return self._view(connection, self._job_auth(connection, previous["job_id"], principal))
            # Coalesce equal requests, including completed jobs whose artifacts still verify.
            candidates = connection.execute("""SELECT DISTINCT router_jobs.* FROM router_jobs JOIN router_requests USING(job_id)
                WHERE router_jobs.owner_id=? AND router_requests.fingerprint=? AND cancelled=0 AND router_jobs.revision=1 ORDER BY created_at DESC""",
                (principal.actor_id, fingerprint)).fetchall()
            job_id = None
            for candidate in candidates:
                states = [row[0] for row in connection.execute("SELECT state FROM router_tasks WHERE job_id=? AND active=1", (candidate["job_id"],))]
                if not any(state in ("failed", "needs_attention", "cancelled") for state in states) and self._completed_artifacts_valid(connection, candidate["job_id"]):
                    job_id = candidate["job_id"]
                    break
            if job_id is None:
                job_id = f"job-{uuid4().hex}"
                connection.execute("INSERT INTO router_jobs(job_id,owner_id,asset_id,priority,max_total_attempts,created_at) VALUES(?,?,?,?,?,?)",
                                   (job_id, principal.actor_id, spec.asset_id, spec.priority, spec.max_total_attempts, self.clock()))
                ids = {step.key: self._new_task(connection, job_id, step) for step in spec.steps}
                for step in spec.steps:
                    for parent in step.depends_on:
                        connection.execute("INSERT INTO router_dependencies VALUES(?,?)", (ids[step.key], ids[parent]))
                self._event(connection, job_id, None, principal.actor_id, "submit", {"fingerprint": fingerprint})
            connection.execute("INSERT INTO router_requests VALUES(?,?,?,?)", (principal.actor_id, request_id, fingerprint, job_id))
            return self._view(connection, self._job_auth(connection, job_id, principal))

    def _completed_artifacts_valid(self, connection, job_id):
        for row in connection.execute("""SELECT envelope_json,result_json FROM router_attempts JOIN router_tasks USING(task_id)
            WHERE job_id=? AND active=1 AND state='succeeded' AND result_json IS NOT NULL""", (job_id,)):
            try:
                self._artifacts(TaskEnvelope.model_validate_json(row[0]), WorkerResult.model_validate_json(row[1]))
            except (StorageError, ValueError):
                return False
        return True

    def _view(self, connection, job):
        rows = connection.execute("SELECT * FROM router_tasks WHERE job_id=? AND active=1 ORDER BY step_key", (job["job_id"],)).fetchall()
        holds = [dict(row) for row in connection.execute("SELECT router_holds.* FROM router_holds JOIN router_tasks USING(task_id) WHERE job_id=?", (job["job_id"],))]
        states = {row["state"] for row in rows}
        if job["cancelled"]:
            state = "cancelled"
        elif holds:
            state = "paused" if job["paused"] else "running"
        elif states <= {"succeeded"}:
            state = "succeeded"
        elif job["paused"]:
            state = "paused"
        elif states & {"queued", "leased", "running", "retry_wait"}:
            state = "running" if states & {"leased", "running"} else "queued"
        elif "needs_attention" in states:
            state = "needs_attention"
        else:
            state = "failed"
        return {"job_id": job["job_id"], "asset_id": job["asset_id"], "revision": job["revision"], "state": state,
                "paused": bool(job["paused"]), "resources": holds,
                "tasks": [{"task_id": row["task_id"], "key": row["step_key"], "stage": json.loads(row["spec_json"])["stage"],
                           "state": row["state"], "attempt": row["attempt"], "lease_epoch": row["lease_epoch"], "error_code": row["error_code"]} for row in rows]}

    def get_job(self, job_id: str, *, principal: Principal) -> dict:
        with self.database.transaction() as connection:
            job = self._job_auth(connection, job_id, principal)
            self._source(connection, job)
            return self._view(connection, job)

    def _expire(self, connection):
        now = self.clock()
        rows = connection.execute("""SELECT router_tasks.*,router_attempts.started_at FROM router_tasks
            JOIN router_attempts ON router_tasks.task_id=router_attempts.task_id AND router_tasks.lease_epoch=router_attempts.lease_epoch
            WHERE state IN ('leased','running')""").fetchall()
        for row in rows:
            step = StepSpec.model_validate_json(row["spec_json"])
            if row["lease_until"] > now and now < row["started_at"] + step.limits.timeout_seconds:
                continue
            state = "retry_wait" if row["attempt"] < step.limits.max_attempts else "failed"
            code = "lease_expired" if row["lease_until"] <= now else "attempt_timeout"
            connection.execute("UPDATE router_tasks SET state=?,ready_at=?,error_code=? WHERE task_id=?", (state, now + self.options.backoff_seconds * 2 ** (row["attempt"] - 1), code, row["task_id"]))
            connection.execute("UPDATE router_holds SET quarantined=1 WHERE task_id=? AND lease_epoch=?", (row["task_id"], row["lease_epoch"]))
            self._event(connection, row["job_id"], row["task_id"], "router", code, {"lease_epoch": row["lease_epoch"]})

    def recover(self, *, principal: Principal):
        if principal.role != "admin":
            raise AccessDenied("administrator capability required")
        with self.database.transaction(write=True) as connection:
            self._expire(connection)

    def _parents(self, connection, task):
        rows = connection.execute("SELECT p.* FROM router_dependencies d JOIN router_tasks p ON p.task_id=d.parent_id WHERE d.task_id=? ORDER BY p.step_key", (task["task_id"],)).fetchall()
        ready, refs = True, []
        for parent in rows:
            if parent["state"] in ("failed", "needs_attention", "cancelled"):
                return "blocked", ()
            if parent["state"] != "succeeded" or connection.execute("SELECT 1 FROM router_attempts WHERE task_id=? AND released_at IS NULL", (parent["task_id"],)).fetchone():
                ready = False
                continue
            attempt = connection.execute("SELECT * FROM router_attempts WHERE task_id=? AND lease_epoch=?", (parent["task_id"], parent["lease_epoch"])).fetchone()
            result = WorkerResult.model_validate_json(attempt["result_json"])
            self._artifacts(TaskEnvelope.model_validate_json(attempt["envelope_json"]), result)
            refs.extend(result.artifacts)
        return ("ready" if ready else "waiting"), tuple(refs)

    def claim(self, *, principal: Principal) -> Lease | None:
        with self.database.transaction(write=True) as connection:
            worker = self._worker(connection, principal)
            self._expire(connection)
            if connection.execute("SELECT paused FROM router_control WHERE id=1").fetchone()[0]:
                return None
            if connection.execute("SELECT 1 FROM router_attempts WHERE worker_id=? AND released_at IS NULL", (principal.actor_id,)).fetchone():
                return None
            held = {row[0] for row in connection.execute("SELECT resource FROM router_holds")}
            cpu = next((f"cpu:{i}" for i in range(self.options.cpu_slots) if f"cpu:{i}" not in held), None)
            if cpu is None:
                return None
            rows = connection.execute("""SELECT router_tasks.* FROM router_tasks JOIN router_jobs USING(job_id)
                WHERE active=1 AND state IN ('queued','retry_wait') AND ready_at<=? AND paused=0 AND cancelled=0
                ORDER BY (priority * ? + (? - router_jobs.created_at)) DESC,router_jobs.created_at,step_key""",
                (self.clock(), self.options.interactive_bonus_seconds, self.clock())).fetchall()
            for task in rows:
                step = StepSpec.model_validate_json(task["spec_json"])
                if step.stage not in json.loads(worker["stages_json"]):
                    continue
                if connection.execute("SELECT 1 FROM router_attempts WHERE task_id=? AND released_at IS NULL", (task["task_id"],)).fetchone():
                    continue
                if step.limits.gpu_slots and "gpu:0" in held:
                    continue
                job = connection.execute("SELECT * FROM router_jobs WHERE job_id=?", (task["job_id"],)).fetchone()
                try:
                    source = self._task_source(connection, job, step)
                    readiness, dependencies = self._parents(connection, task)
                except (AccessDenied, NotFound, StorageError, ValueError):
                    connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='input_unavailable' WHERE task_id=?", (task["task_id"],))
                    continue
                if readiness == "blocked":
                    connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='dependency_unavailable' WHERE task_id=?", (task["task_id"],))
                    continue
                if readiness != "ready":
                    continue
                count = connection.execute("SELECT count(*) FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?", (task["job_id"],)).fetchone()[0]
                if count >= job["max_total_attempts"] or task["attempt"] >= step.limits.max_attempts:
                    connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='attempt_budget' WHERE task_id=?", (task["task_id"],))
                    continue
                definition = self.stages.get(step.stage)
                if definition is None:
                    connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='stage_unavailable' WHERE task_id=?", (task["task_id"],))
                    continue
                try:
                    self._validate_steps((step,))
                except ValueError:
                    connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='stage_version_unavailable' WHERE task_id=?", (task['task_id'],))
                    continue
                effective = {row["field"]: json.loads(row["payload"]) for row in connection.execute("SELECT * FROM effective_values WHERE asset_id=?", (job["asset_id"],)) if row["field"] in step.target_fields}
                fingerprint = _digest({"source": source["blob_sha256"], "asset_id": job["asset_id"], "owner": job["owner_id"],
                                       "dependencies": [item.model_dump(mode="json") for item in dependencies], "config": step.config,
                                       "stage": step.stage, "limits": step.limits.model_dump(),
                                       "versions": step.versions.model_dump(), "implementation": definition.implementation_version,
                                       "prompt": step.prompt_version, "fields": step.target_fields,
                                       "field_values": effective if definition.metadata_sensitive else {}})
                attempt, epoch = task["attempt"] + 1, task["lease_epoch"] + 1
                envelope = TaskEnvelope(task_id=task["task_id"], job_id=task["job_id"], asset_id=job["asset_id"],
                    asset_revision=source["revision"], stage=step.stage, attempt=attempt, lease_epoch=epoch,
                    input_fingerprint=fingerprint, input_refs=(ArtifactRef(kind="source_blob", ref=f"assets/{job['asset_id']}/{source['blob_sha256']}", sha256=source["blob_sha256"]), *dependencies),
                    versions=step.versions, target_fields=step.target_fields, limits=step.limits,
                    output_prefix=f"jobs/{task['job_id']}/runs/{task['task_id']}/attempt-{attempt}/")
                now, until = self.clock(), self.clock() + self.options.lease_seconds
                connection.execute("INSERT INTO router_attempts(task_id,lease_epoch,attempt,worker_id,envelope_json,config_json,started_at) VALUES(?,?,?,?,?,?,?)",
                                   (task["task_id"], epoch, attempt, principal.actor_id, envelope.model_dump_json(), encode(step.config), now))
                connection.execute("UPDATE router_tasks SET state='leased',attempt=?,lease_epoch=?,lease_owner=?,lease_until=?,error_code=NULL WHERE task_id=?",
                                   (attempt, epoch, principal.actor_id, until, task["task_id"]))
                for resource in (cpu, *(('gpu:0',) if step.limits.gpu_slots else ())):
                    connection.execute("INSERT INTO router_holds(resource,task_id,lease_epoch,worker_id) VALUES(?,?,?,?)", (resource, task["task_id"], epoch, principal.actor_id))
                self._event(connection, job["job_id"], task["task_id"], principal.actor_id, "claim", {"attempt": attempt, "lease_epoch": epoch})
                return Lease(task=envelope, config=step.config, lease_until=until)
        return None

    def _lease(self, connection, task_id, epoch, principal):
        task = connection.execute("SELECT * FROM router_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None or task["lease_owner"] != principal.actor_id or task["lease_epoch"] != epoch:
            return None
        job = connection.execute("SELECT * FROM router_jobs WHERE job_id=?", (task["job_id"],)).fetchone()
        if not task["active"] or task["state"] not in ("leased", "running") or task["lease_until"] <= self.clock() or job["cancelled"]:
            return None
        return task

    def heartbeat(self, task_id: str, epoch: int, *, principal: Principal) -> dict:
        with self.database.transaction(write=True) as connection:
            self._worker(connection, principal)
            self._expire(connection)
            task = self._lease(connection, task_id, epoch, principal)
            if task is None:
                return {"alive": False, "cancel_requested": True}
            job = connection.execute("SELECT * FROM router_jobs WHERE job_id=?", (task["job_id"],)).fetchone()
            try:
                self._task_source(connection, job, StepSpec.model_validate_json(task["spec_json"]))
            except (AccessDenied, NotFound):
                connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='source_access_revoked' WHERE task_id=?", (task_id,))
                connection.execute("UPDATE router_holds SET quarantined=1 WHERE task_id=?", (task_id,))
                return {"alive": False, "cancel_requested": True}
            until = self.clock() + self.options.lease_seconds
            connection.execute("UPDATE router_tasks SET state='running',lease_until=? WHERE task_id=?", (until, task_id))
            return {"alive": True, "cancel_requested": False, "lease_until": until}

    def _artifacts(self, task: TaskEnvelope, result: WorkerResult):
        validate_result_for_task(task, result)
        definition = self.stages[task.stage]
        if definition.require_artifact and result.status in ("succeeded", "partial", "not_applicable") and not result.artifacts:
            raise ValueError("stage requires a real output artifact")
        inputs = set(task.input_refs)
        for artifact in (*result.artifacts, *(item.artifact for item in result.evidence)):
            if artifact in inputs and artifact not in result.artifacts:
                continue
            if not artifact.ref.startswith(task.output_prefix):
                raise ValueError("result artifacts must belong to the leased attempt")
            with self.files.open_read(artifact.ref) as stream:
                digest = sha256()
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
                if digest.hexdigest() != artifact.sha256:
                    raise ValueError("result artifact hash mismatch")
        if any(item.source_sha256 != task.input_refs[0].sha256 for item in result.evidence):
            raise ValueError("evidence source does not match the registered file")

    def _rebase(self, connection, envelope, source):
        if source["revision"] == envelope.asset_revision:
            return envelope
        definition = self.stages[envelope.stage]
        if definition.metadata_sensitive:
            if not definition.allow_disjoint_rebase:
                raise RouterConflict("asset revision changed")
            changes = connection.execute("SELECT * FROM field_revisions WHERE asset_id=? AND revision>? ORDER BY revision", (envelope.asset_id, envelope.asset_revision)).fetchall()
            if len(changes) != source["revision"] - envelope.asset_revision:
                raise RouterConflict("missing revision history")
            for change in changes:
                before, after = json.loads(change["before_json"]), json.loads(change["after_json"])
                if "values" in before and "values" in after:
                    left = {item["field"]: item for item in before["values"]}
                    right = {item["field"]: item for item in after["values"]}
                    if any(left.get(field) != right.get(field) for field in envelope.target_fields):
                        raise RouterConflict("a target field was revised")
                elif not (("permissions" in before and "permissions" in after) or ("archived" in before and "archived" in after)):
                    raise RouterConflict("revision kind cannot be safely rebased")
        value = json.loads(envelope.model_dump_json())
        value["asset_revision"] = source["revision"]
        return TaskEnvelope.model_validate_json(encode(value))

    def complete(self, result: WorkerResult, *, principal: Principal) -> dict:
        digest = _digest(result.model_dump(mode="json"))
        with self.database.transaction(write=True) as connection:
            self._worker(connection, principal)
            self._expire(connection)
            old = connection.execute("SELECT * FROM router_attempts WHERE task_id=? AND lease_epoch=?", (result.task_id, result.lease_epoch)).fetchone()
            if old and old["worker_id"] == principal.actor_id and old["result_digest"]:
                same = old["result_digest"] == digest
                reason = "duplicate" if same else "result_conflict"
                connection.execute("INSERT INTO router_receipts(task_id,lease_epoch,worker_id,digest,accepted,reason,received_at) VALUES(?,?,?,?,?,?,?)",
                                   (result.task_id, result.lease_epoch, principal.actor_id, digest, int(same), reason, self.clock()))
                return {"accepted": same, "duplicate": same, "reason": reason}
            task = self._lease(connection, result.task_id, result.lease_epoch, principal)
            reason = None if task else "stale_lease"
            adopted = None
            if task:
                job = connection.execute("SELECT * FROM router_jobs WHERE job_id=?", (task["job_id"],)).fetchone()
                envelope = TaskEnvelope.model_validate_json(old["envelope_json"])
                try:
                    source = self._task_source(connection, job, StepSpec.model_validate_json(task["spec_json"]))
                    adopted = self._rebase(connection, envelope, source)
                    self._artifacts(envelope, result)
                    if self._lease(connection, result.task_id, result.lease_epoch, principal) is None or self.clock() >= old["started_at"] + envelope.limits.timeout_seconds:
                        raise RouterConflict("execution expired while validating its artifacts")
                    if result.elapsed_seconds > envelope.limits.timeout_seconds:
                        raise ValueError("result exceeds its execution budget")
                    # A savepoint makes catalogue adoption and queue acknowledgement a single commit.
                    connection.execute("SAVEPOINT suggestion_adoption")
                    try:
                        if result.status == "succeeded" and result.suggestions:
                            self.catalog._record_suggestions_tx(connection, adopted, result, principal=principal,
                                                                delegate=Principal(actor_id=job["owner_id"]))
                    except BaseException:
                        connection.execute("ROLLBACK TO suggestion_adoption")
                        connection.execute("RELEASE suggestion_adoption")
                        raise
                    connection.execute("RELEASE suggestion_adoption")
                except (ValueError, StorageError, AccessDenied, NotFound, RevisionConflict, UnsupportedRevision):
                    reason = "invalid_or_stale_result"
                    connection.execute("UPDATE router_tasks SET state='needs_attention',error_code=? WHERE task_id=?", (reason, result.task_id))
                    connection.execute("UPDATE router_holds SET quarantined=1 WHERE task_id=?", (result.task_id,))
                if reason is None:
                    step = StepSpec.model_validate_json(task["spec_json"])
                    state, code = ("succeeded", None) if result.status in ("succeeded", "not_applicable") else ("needs_attention", "partial_result")
                    if result.status == "failed":
                        code = result.errors[0].code
                        retry = all(error.retryable for error in result.errors) and not any(
                            error.code in {"auth_failed", "invalid_media", "unpublished_vocabulary", "schema_error"} for error in result.errors)
                        if code == "oom":
                            definition = self.stages[step.stage]
                            retry = task["reductions"] == 0 and definition.reduce_config is not None
                            if retry:
                                reduced = definition.reduce_config(step.config)
                                definition.config_model.model_validate_json(encode(reduced))
                                if reduced == step.config:
                                    retry = False
                                else:
                                    data = json.loads(step.model_dump_json())
                                    data["config"] = reduced
                                    step = StepSpec.model_validate_json(encode(data))
                                    connection.execute("UPDATE router_tasks SET spec_json=?,reductions=reductions+1 WHERE task_id=?", (step.model_dump_json(), task["task_id"]))
                        state = "retry_wait" if retry and task["attempt"] < step.limits.max_attempts else "failed" if retry else "needs_attention"
                    safe = json.loads(result.model_dump_json())
                    for error in safe["errors"]:
                        error["message"] = f"Worker reported {error['code']}"
                    connection.execute("UPDATE router_attempts SET result_json=?,result_digest=?,adopted_revision=? WHERE task_id=? AND lease_epoch=?",
                                       (encode(safe), digest, adopted.asset_revision, task["task_id"], result.lease_epoch))
                    connection.execute("UPDATE router_tasks SET state=?,error_code=?,ready_at=? WHERE task_id=?", (state, code, self.clock() + self.options.backoff_seconds * (2 ** max(0, task["attempt"] - 1)), task["task_id"]))
                    self._event(connection, task["job_id"], task["task_id"], principal.actor_id, "result", {"state": state, "digest": digest, "adopted_revision": adopted.asset_revision})
            connection.execute("INSERT INTO router_receipts(task_id,lease_epoch,worker_id,digest,accepted,reason,received_at) VALUES(?,?,?,?,?,?,?)",
                               (result.task_id, result.lease_epoch, principal.actor_id, digest, int(reason is None), reason or "accepted", self.clock()))
            return {"accepted": reason is None, "duplicate": False, "reason": reason or "accepted"}

    def release(self, task_id: str, epoch: int, *, principal: Principal) -> bool:
        """Trusted executor acknowledgement: call only after its handler/resources actually stop."""
        with self.database.transaction(write=True) as connection:
            self._worker(connection, principal, enabled=False)
            self._expire(connection)
            attempt = connection.execute("SELECT * FROM router_attempts WHERE task_id=? AND lease_epoch=? AND worker_id=?", (task_id, epoch, principal.actor_id)).fetchone()
            if attempt is None:
                raise AccessDenied("attempt does not belong to this worker")
            if attempt["released_at"] is not None:
                return False
            connection.execute("UPDATE router_attempts SET released_at=? WHERE task_id=? AND lease_epoch=?", (self.clock(), task_id, epoch))
            connection.execute("DELETE FROM router_holds WHERE task_id=? AND lease_epoch=? AND worker_id=?", (task_id, epoch, principal.actor_id))
            task = connection.execute("SELECT * FROM router_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task["lease_epoch"] == epoch and task["state"] in ("leased", "running"):
                connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='executor_stopped_without_result' WHERE task_id=?", (task_id,))
            return True

    def confirm_stopped(self, task_id: str, epoch: int, *, principal: Principal):
        if principal.role != "admin":
            raise AccessDenied("administrator capability required")
        if self.process_stopped is None or not self.process_stopped(task_id, epoch):
            raise RouterConflict("no trusted evidence that this execution has stopped")
        with self.database.transaction(write=True) as connection:
            self._expire(connection)
            row = connection.execute("SELECT * FROM router_attempts WHERE task_id=? AND lease_epoch=?", (task_id, epoch)).fetchone()
            if row is None:
                raise NotFound("attempt is unavailable")
            connection.execute("UPDATE router_attempts SET released_at=COALESCE(released_at,?) WHERE task_id=? AND lease_epoch=?", (self.clock(), task_id, epoch))
            connection.execute("DELETE FROM router_holds WHERE task_id=? AND lease_epoch=?", (task_id, epoch))
            task = connection.execute("SELECT * FROM router_tasks WHERE task_id=?", (task_id,)).fetchone()
            if task["lease_epoch"] == epoch and task["state"] in ("leased", "running"):
                step = StepSpec.model_validate_json(task["spec_json"])
                state = "retry_wait" if task["attempt"] < step.limits.max_attempts else "failed"
                connection.execute("UPDATE router_tasks SET state=?,error_code='process_stopped',ready_at=? WHERE task_id=?",
                                   (state, self.clock() + self.options.backoff_seconds, task_id))
            self._event(connection, task["job_id"], task_id, principal.actor_id, "process_stopped", {"lease_epoch": epoch})

    def pause(self, job_id: str, *, principal: Principal, paused=True):
        with self.database.transaction(write=True) as connection:
            self._job_auth(connection, job_id, principal)
            connection.execute("UPDATE router_jobs SET paused=? WHERE job_id=?", (int(paused), job_id))
            self._event(connection, job_id, None, principal.actor_id, "pause" if paused else "resume", {})

    def pause_dispatch(self, *, principal: Principal, paused=True):
        if principal.role != "admin":
            raise AccessDenied("administrator capability required")
        with self.database.transaction(write=True) as connection:
            connection.execute("UPDATE router_control SET paused=? WHERE id=1", (int(paused),))

    def cancel(self, job_id: str, *, principal: Principal):
        with self.database.transaction(write=True) as connection:
            self._job_auth(connection, job_id, principal)
            connection.execute("UPDATE router_jobs SET cancelled=1 WHERE job_id=?", (job_id,))
            connection.execute("UPDATE router_tasks SET state='cancelled',error_code='cancel_requested' WHERE job_id=? AND state NOT IN ('succeeded','failed','needs_attention','cancelled')", (job_id,))
            connection.execute("UPDATE router_holds SET quarantined=1 WHERE task_id IN (SELECT task_id FROM router_tasks WHERE job_id=?)", (job_id,))
            self._event(connection, job_id, None, principal.actor_id, "cancel", {})

    def replan(self, job_id: str, patches: tuple[StepSpec, ...], *, expected_revision: int, principal: Principal):
        with self.database.transaction(write=True) as connection:
            return self._replan_tx(connection, job_id, patches, expected_revision=expected_revision, principal=principal)

    def _replan_tx(self, connection, job_id, patches, *, expected_revision, principal):
        """Trusted transaction boundary for atomic domain operations and audit receipts."""
        self._validate_steps(patches)
        if not patches or len({step.key for step in patches}) != len(patches):
            raise ValueError("replan requires unique step patches")
        job = self._job_auth(connection, job_id, principal)
        self._source(connection, job)
        if job["revision"] != expected_revision or job["cancelled"]:
            raise RouterConflict("job revision changed or job was cancelled")
        old = {row["step_key"]: row for row in connection.execute("SELECT * FROM router_tasks WHERE job_id=? AND active=1", (job_id,))}
        specs = {key: StepSpec.model_validate_json(row["spec_json"]) for key, row in old.items()}
        specs.update({step.key: step for step in patches})
        JobSpec(asset_id=job["asset_id"], steps=tuple(specs.values()))
        for spec in specs.values():
            self._task_source(connection, job, spec)
        changed = {step.key for step in patches}
        while True:
            more = {key for key, spec in specs.items() if set(spec.depends_on) & changed}
            if more <= changed:
                break
            changed |= more
        ids = {key: row["task_id"] for key, row in old.items() if key not in changed}
        for key in changed:
            prior = old.get(key)
            if prior:
                connection.execute("UPDATE router_tasks SET active=0,state=CASE WHEN state IN ('leased','running','queued','retry_wait') THEN 'cancelled' ELSE state END WHERE task_id=?", (prior["task_id"],))
                connection.execute("UPDATE router_holds SET quarantined=1 WHERE task_id=?", (prior["task_id"],))
            ids[key] = self._new_task(connection, job_id, specs[key], prior["generation"] + 1 if prior else 1)
        for key in changed:
            for parent in specs[key].depends_on:
                connection.execute("INSERT INTO router_dependencies VALUES(?,?)", (ids[key], ids[parent]))
        connection.execute("UPDATE router_jobs SET revision=revision+1 WHERE job_id=?", (job_id,))
        self._event(connection, job_id, None, principal.actor_id, "replan", {"changed_keys": sorted(changed)})
        return self._view(connection, self._job_auth(connection, job_id, principal))

    @contextmanager
    def open_input(self, task_id: str, epoch: int, *, principal: Principal) -> Iterator:
        with self.database.transaction(write=True) as connection:
            self._worker(connection, principal)
            self._expire(connection)
            task = self._lease(connection, task_id, epoch, principal)
            if task is None:
                raise RouterConflict("input lease is no longer active")
            job = connection.execute("SELECT * FROM router_jobs WHERE job_id=?", (task["job_id"],)).fetchone()
            source = self._source(connection, job)
        delegate = Principal(actor_id=job["owner_id"])
        if source['kind']=='derived':
            from media_tag.files.service import FileService
            files = FileService(self)
            opened = files.open(source['asset_id'], source['blob_sha256'], principal=delegate)
            try:
                yield opened.stream
                files.authorize(source['asset_id'], source['blob_sha256'], principal=delegate, for_bytes=True)
            finally:
                opened.close()
        else:
            with self.catalog.open_content(job["asset_id"], principal=delegate) as stream:
                yield stream
