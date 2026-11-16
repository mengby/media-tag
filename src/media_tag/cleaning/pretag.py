"""Screen selected original media before semantic tagging, using the job's evidence."""
from hashlib import sha256
from io import BytesIO
import json
import re

from PIL import Image

from media_tag.contracts.models import ArtifactRef, ContractModel, TaskEnvelope, TaskLimits, Versions, WorkerResult
from .watermark import PROMPT_VERSION
from .service import edge_variance
from media_tag.files.service import FileError
from media_tag.router.models import StepSpec
from media_tag.storage import IntegrityError
from media_tag.workers.probe import MediaError

STAGE = "pretag_cleaning"
VERSION = "pretag-screen-v1"
WATERMARK = re.compile(
    r"抖音|快手|小红书|哔哩哔哩|微信视频号|摄图网|视觉中国|千图网|站酷海洛|图虫|样片|"
    r"©|版权所有|保留所有权利|all\s+rights\s+reserved|sample\s+use\s+only|"
    r"shutterstock|istock|getty\s*images|adobe\s*stock|tiktok|douyin|watermark|"
    r"depositphotos|dreamstime|alamy|vecteezy|freepik|123rf|pond5|envato|"
    r"(?:https?://|www\.)[\w.-]+|(?<!\w)@[\w.-]{2,}", re.I)


class ScreenOptions(ContractModel):
    use_ocr: bool


def plan(connection, asset_id, steps, definition):
    by_key = {s.key: s for s in steps}
    if not all(k in by_key and by_key[k].stage == k for k in ("probe", "evidence")):
        return steps
    enrolled = connection.execute("""SELECT 1 FROM uploads u JOIN assets a USING(asset_id)
        WHERE u.asset_id=? AND a.kind='original' AND u.state='completed' AND u.owner_id=a.owner_id
        AND json_extract(u.spec_json,'$.pretag_cleaning')=1
        AND json_extract(u.validation_json,'$.kind') IN ('image','video','animation','gif') LIMIT 1""",
        (asset_id,)).fetchone()
    if not enrolled:
        return steps
    evidence = by_key["evidence"]
    config = {**evidence.config, "sample_across_duration": True, "max_frames": min(3, evidence.config.get("max_frames", 3))}
    by_key["evidence"] = evidence.model_copy(update={"config": config})
    use_ocr = "ocr" in by_key and by_key["ocr"].stage == "ocr"
    parents = ("probe", "evidence", "ocr") if use_ocr else ("probe", "evidence")
    model = definition.required_model_revision
    by_key[STAGE] = StepSpec(key=STAGE, stage=STAGE, depends_on=parents, config={"use_ocr": use_ocr},
        versions=Versions(rules_version=VERSION, preprocess_version=VERSION, model_revision=model),
        prompt_version=PROMPT_VERSION if model else None, limits=TaskLimits(gpu_slots=1 if model else 0))
    for key, step in by_key.items():
        if step.stage in ("visual_fields", "local_topics", "precision", "clip", "face") and STAGE not in step.depends_on:
            by_key[key] = step.model_copy(update={"depends_on": (*step.depends_on, STAGE)})
    return tuple(by_key.values())


def checked_bytes(backend, ref, limit):
    with backend.open_read(ref.ref) as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit or sha256(raw).hexdigest() != ref.sha256:
        raise IntegrityError("pretag evidence integrity mismatch")
    return raw


def one_report(backend, refs, kind):
    matches = [ref for ref in refs if ref.kind == kind]
    if len(matches) != 1:
        raise IntegrityError("one bound " + kind + " required")
    return json.loads(checked_bytes(backend, matches[0], 8 * 1024**2))


def retry_failed_screen(router, job_id, principal):
    """Legacy batch submission can retry a failed screen even with modern analysis disabled."""
    with router.database.transaction(write=True) as conn:
        job = router._job_auth(conn, job_id, principal)
        if job["cancelled"] or job["paused"]:
            return
        row = conn.execute("""SELECT task_id,state FROM router_tasks WHERE job_id=? AND active=1
                AND step_key=? AND json_extract(spec_json,'$.stage')=?""",(job_id,STAGE,STAGE)).fetchone()
        if not row:
            return
        failed = conn.execute("""WITH RECURSIVE prerequisites(id) AS (
            SELECT ? UNION SELECT d.parent_id FROM router_dependencies d JOIN prerequisites p ON d.task_id=p.id)
            SELECT t.spec_json FROM router_tasks t JOIN prerequisites p ON t.task_id=p.id
            WHERE t.job_id=? AND t.active=1 AND t.state IN ('failed','needs_attention')""",
            (row["task_id"],job_id)).fetchall()
        if failed:
            patches = tuple(StepSpec.model_validate_json(item["spec_json"]) for item in failed)
            router._replan_tx(conn,job_id,patches,expected_revision=job["revision"],principal=principal)


def screen(backend, refs, options, stopped, visual=None):
    source = refs[0].sha256
    technical = one_report(backend, refs, "technical_report")
    evidence = one_report(backend, refs, "evidence_manifest")
    frames = evidence.get("frames", [])
    if evidence.get("status") != "succeeded" or not 1 <= len(frames) <= 12:
        raise MediaError("missing_cleaning_frames", "cleaning requires decoded visual evidence")
    observed = []
    if options.use_ocr:
        ocr = one_report(backend, refs, "model_report")
        if ocr.get("kind") != "ocr" or ocr.get("source_sha256") != source or len(ocr["frames"]) != len(frames):
            raise IntegrityError("OCR source or frame coverage changed")
        observed = ocr["frames"]
    samples, hits, visual_inputs = [], [], []
    for i, frame in enumerate(frames):
        if stopped.is_set():
            raise MediaError("cancelled", "cleaning stopped")
        ref = ArtifactRef(**{k:frame["artifact"][k] for k in ("kind","ref","sha256")})
        if ref not in refs or frame["source_sha256"] != source:
            raise IntegrityError("cleaning frame binding changed")
        with Image.open(BytesIO(checked_bytes(backend, ref, 16 * 1024**2))) as im:
            if im.format != "PNG" or im.size != (frame["width"], frame["height"]) or im.width*im.height > 2048**2:
                raise IntegrityError("invalid cleaning frame")
            variance = round(edge_variance(im), 3)
        sample = {"frame_sha256":ref.sha256, "seconds":frame["normalized_seconds"],
                  "width":frame["width"], "height":frame["height"], "edge_variance":variance}
        samples.append(sample)
        visual_inputs.append({"source_sha256":source,"artifact":ref.model_dump(),"normalized_seconds":sample["seconds"]})
        if observed:
            item = observed[i]
            if (item["artifact"] != ref.model_dump() or item["normalized_seconds"] != sample["seconds"]
                    or (item["width"],item["height"]) != (sample["width"],sample["height"])):
                raise IntegrityError("OCR coordinates do not match sampled frame")
            observation = item["observation"]
            if observation["state"] not in ("observed","no_detection_in_sample"):
                raise IntegrityError("OCR frame was not screened")
            for line in observation["lines"]:
                if WATERMARK.search(line["text"]):
                    hits.append({"sample":i,"text":line["text"][:256],"quadrilateral":line["quadrilateral"],
                                 "recognition_score":line["recognition_score"]})
    visual_results = visual(visual_inputs) if visual else []
    if hits or any(r["state"]=="candidate_review" for r in visual_results):
        state = "candidate_review"
    elif any(r["state"]=="uncertain" for r in visual_results) or evidence.get("unavailable_requested_seconds"):
        state = "needs_review"
    elif observed and visual_results:
        state = "no_hit_in_samples"
    else:
        state = "no_text_hit_in_samples" if observed else "awaiting_ocr"
    return {"schema_version":VERSION, "source_sha256":source, "original_preserved":True,
            "media_kind":technical["kind"],"width":technical["width"],"height":technical["height"],
            "duration_seconds":technical["duration_seconds"], "samples":samples,
            "quality_scope":"sampled_frames","sharpness_calibrated":False,
            "watermark":{"state":state,"candidates":hits,"ocr":"screened" if observed else "unavailable",
                         "visual_observations":visual_results,"graphic_logos":"sampled" if visual_results else "unassessed"},
            "unavailable_requested_seconds":evidence.get("unavailable_requested_seconds", []),
            "full_video_observed":False,"license_status":"unverified"}


def current_screen(files, asset_id, principal, *, frame_sha=None, task_id=None):
    router = files.router
    with router.database.transaction() as conn:
        asset = router.catalog._authorize(conn, asset_id, principal)
        if files._gate(conn, asset):
            raise FileError(409, "source_unavailable")
        row = conn.execute("""SELECT j.job_id,j.cancelled,t.task_id,t.state,a.envelope_json,a.result_json,a.released_at
            FROM router_jobs j JOIN router_tasks t USING(job_id)
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE j.asset_id=? AND j.owner_id=? AND t.active=1 AND t.step_key=? AND json_extract(t.spec_json,'$.stage')=?
            ORDER BY j.created_at DESC,j.rowid DESC LIMIT 1""",(asset_id,asset["owner_id"],STAGE,STAGE)).fetchone()
        state = "not_requested" if row is None else "cancelled" if row["cancelled"] else row["state"]
        if row and state == "succeeded" and row["released_at"] is None:
            state = "awaiting_release"
        if state != "succeeded":
            if frame_sha is not None:
                raise FileError(404, "cleaning_frame_unavailable")
            return {"state":state, **({"job_id":row["job_id"],"task_id":row["task_id"]} if row else {})}
        task = TaskEnvelope.model_validate_json(row["envelope_json"])
        result = WorkerResult.model_validate_json(row["result_json"])
        if (task.stage != STAGE or task.task_id != row["task_id"] or task.asset_id != asset_id
                or task.job_id != row["job_id"] or task.input_refs[0].sha256 != asset["blob_sha256"]
                or result.status != "succeeded"):
            raise FileError(409, "cleaning_report_unavailable")
        router._artifacts(task, result)
        report = one_report(router.files, result.artifacts, "pretag_cleaning_report")
        if report["schema_version"] != VERSION or report["source_sha256"] != asset["blob_sha256"]:
            raise FileError(409, "cleaning_report_unavailable")
        if frame_sha is None:
            return {"state":"succeeded","job_id":row["job_id"],"task_id":task.task_id,"report":report}
        if task_id != task.task_id or not any(s["frame_sha256"] == frame_sha for s in report["samples"]):
            raise FileError(404, "cleaning_frame_unavailable")
        ref = next((r for r in task.input_refs if r.kind in ("image","frame") and r.sha256==frame_sha), None)
        if ref is None:
            raise FileError(409, "cleaning_frame_unavailable")
        return checked_bytes(router.files, ref, 16 * 1024**2)
