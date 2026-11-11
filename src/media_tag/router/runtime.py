"""Bounded local CPU handlers and a heartbeat executor; no inference or daemon on import."""

from hashlib import sha256
import json
import os
from pathlib import Path
import threading
import time

from media_tag.catalog import Principal
from media_tag.contracts.models import ArtifactRef, ErrorDetail, WorkerResult
from media_tag.storage import IntegrityError
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.evidence import EVIDENCE_VERSION, EvidenceOptions, _directory, evidence_records, extract_frames
from media_tag.workers.probe import PROBE_VERSION, MediaError, ProbeOptions, Toolchain, file_sha256, probe_media
from .models import Lease, StageDefinition
from .service import Router


def cpu_stages(tools: Toolchain) -> dict[str, StageDefinition]:
    identity = sha256(json.dumps(tools.identity, sort_keys=True).encode()).hexdigest()
    return {
        "probe": StageDefinition(ProbeOptions, f"{PROBE_VERSION}:{identity}", metadata_sensitive=False),
        "evidence": StageDefinition(EvidenceOptions, f"{EVIDENCE_VERSION}:{identity}", metadata_sensitive=False),
    }


def _artifact(path, workspace, kind):
    return ArtifactRef(kind=kind, ref=path.relative_to(workspace).as_posix(), sha256=file_sha256(path))


class CPUHandlers:
    def __init__(self, router: Router, worker: Principal, tools: Toolchain):
        self.router, self.worker, self.tools = router, worker, tools

    def __call__(self, lease: Lease, stopped: threading.Event) -> dict:
        task, workspace = lease.task, self.router.workspace
        parent = _directory(workspace, task.output_prefix.rstrip("/").rsplit("/", 1)[0])
        output = parent / f"attempt-{task.attempt}"
        output.mkdir()  # Never overwrite a prior attempt, even after executor restart.
        (output / "README.md").write_text("Private D03 execution artifacts; source media is read only.\n", encoding="utf-8")
        (output / "task.json").write_text(task.model_dump_json(indent=2), encoding="utf-8")
        (output / "config.json").write_text(json.dumps(lease.config, sort_keys=True), encoding="utf-8")
        # Share verified source copies within the job; keep Windows CAS paths below deep attempt paths.
        inputs = _directory(workspace, f"jobs/{task.job_id}/inputs")
        store = FilesystemBackend(inputs, writable=True)
        if stopped.is_set():
            raise MediaError("cancelled", "executor stopped at stage boundary")
        with self.router.open_input(task.task_id, task.lease_epoch, principal=self.worker) as source:
            maximum = ProbeOptions.model_validate_json(json.dumps(lease.config)).max_input_bytes if task.stage == "probe" else ProbeOptions().max_input_bytes
            if os.fstat(source.fileno()).st_size > maximum:
                raise MediaError("input_budget", "registered source exceeds byte budget")
            blob = store.ingest(source)
            if blob.sha256 != task.input_refs[0].sha256:
                raise IntegrityError("source changed before staging")
        with store.open_verified(blob.object_key, blob.sha256, blob.size_bytes) as stream:
            path = Path(stream.name)
            if task.stage == "probe":
                options = ProbeOptions.model_validate_json(json.dumps(lease.config))
                data = probe_media(path, self.tools, options).as_dict()
                failures = [ErrorDetail(code=track["reason"], message="audio scan did not complete")
                            for track in data["audio_tracks"] if track["reason"] in
                            {"decode_failed", "tool_timeout", "output_budget", "tool_unavailable", "unsupported_format"}]
                target = output / "technical.json"
                target.write_text(json.dumps(data, ensure_ascii=True, sort_keys=True, allow_nan=False), encoding="utf-8")
                return {"status": "partial" if failures else "succeeded", "errors": tuple(failures),
                        "artifacts": (_artifact(target, workspace, "technical_report"),)}
            if task.stage != "evidence":
                raise MediaError("stage_unavailable", "CPU handler is not registered")
            probes = [item for item in task.input_refs if item.kind == "technical_report"]
            if len(probes) != 1:
                raise MediaError("missing_probe", "evidence requires one successful probe dependency")
            with self.router.files.open_read(probes[0].ref) as report:
                if sha256(report.read()).hexdigest() != probes[0].sha256:
                    raise IntegrityError("probe dependency hash changed")
                report.seek(0)
                info = json.load(report)
            options = EvidenceOptions.model_validate_json(json.dumps(lease.config))
            data = extract_frames(path, info, output, workspace, self.tools, options, blob.sha256)
            target = output / "evidence.json"
            target.write_text(json.dumps(data, sort_keys=True, allow_nan=False), encoding="utf-8")
            evidence = evidence_records({"evidence": {"status": data["status"], "fingerprint": task.input_fingerprint, "data": data}})
            artifacts = (_artifact(target, workspace, "evidence_manifest"), *(item.artifact for item in evidence))
            return {"status": data["status"], "artifacts": artifacts, "evidence": evidence, "reason": data.get("reason")}


class LocalExecutor:
    """Trusted handlers must join/clean up their child processes before returning or raising.

    Cancellation is cooperative at a stage boundary. A hung/killed executor retains its
    resource holds until a trusted process probe confirms exit; heartbeat loss never frees GPU.
    """

    def __init__(self, router: Router, principal: Principal, handler, *, after_release=None, before_acquire=None):
        self.router, self.principal, self.handler = router, principal, handler
        self.after_release = after_release
        self.before_acquire = before_acquire

    def run_once(self) -> dict | None:
        if self.before_acquire:
            self.before_acquire()
        lease = self.router.claim(principal=self.principal)
        if lease is None:
            return None
        stopped, finished = threading.Event(), threading.Event()
        task = lease.task
        def beat():
            while not finished.wait(self.router.options.heartbeat_seconds):
                try:
                    if not self.router.heartbeat(task.task_id, task.lease_epoch, principal=self.principal)["alive"]:
                        stopped.set()
                        return
                except Exception:
                    stopped.set()  # Stop at the next safe boundary if the control plane is unavailable.
                    return
        thread = threading.Thread(target=beat, name="media-tag-heartbeat", daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            try:
                payload = self.handler(lease, stopped)
                if stopped.is_set():
                    raise MediaError("cancelled", "executor stopped at stage boundary")
            except Exception as exc:
                from media_tag.models.contracts import ModelFailure
                code = exc.code if isinstance(exc, (MediaError, ModelFailure)) else "executor_error"
                payload = {"status": "failed", "errors": (ErrorDetail(code=code, message="stage execution failed",
                           retryable=code in {"tool_timeout", "tool_unavailable"}),)}
            result = WorkerResult(task_id=task.task_id, attempt=task.attempt, lease_epoch=task.lease_epoch,
                                  input_fingerprint=task.input_fingerprint, versions=task.versions,
                                  elapsed_seconds=time.monotonic() - started, **payload)
            return self.router.complete(result, principal=self.principal)
        finally:
            finished.set()
            thread.join()
            self.router.release(task.task_id, task.lease_epoch, principal=self.principal)
            if self.after_release:
                self.after_release()
