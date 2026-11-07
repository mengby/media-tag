import json

import pytest
from pydantic import ValidationError

from media_tag.contracts.models import (
    ArtifactRef, EffectiveValue, Evidence, FieldValue, TagSuggestion, TaskEnvelope,
    WorkerResult, validate_result_for_task,
)

HASH = "a" * 64  # Synthetic identifier, not a real media hash.


def task_data():
    return {
        "task_id": "synthetic-task", "job_id": "synthetic-job", "asset_id": "synthetic-asset",
        "asset_revision": 1, "stage": "synthetic-probe", "attempt": 1, "lease_epoch": 2,
        "input_fingerprint": HASH,
        "input_refs": [{"kind": "synthetic", "ref": "inputs/synthetic.bin", "sha256": HASH}],
        "versions": {"rules_version": "synthetic-v1", "preprocess_version": "synthetic-v1"},
        "target_fields": ["synthetic_field"],
        "output_prefix": "jobs/synthetic-job/runs/synthetic-task/attempt-1/",
    }


def result_data():
    task = task_data()
    return {key: task[key] for key in ("task_id", "attempt", "lease_epoch", "input_fingerprint", "versions")} | {
        "status": "succeeded", "elapsed_seconds": 0.1,
    }


@pytest.mark.parametrize("state", ["unknown", "no_match", "not_applicable", "failed"])
def test_unknown_failure_no_match_not_applicable_never_become_values(state):
    value = FieldValue(state=state, reason="synthetic reason")
    assert value.values is None
    with pytest.raises(ValidationError):
        FieldValue(state=state, values=("false",), reason="synthetic reason")
    with pytest.raises(ValidationError):
        FieldValue(state=state)


def test_observed_suggestion_requires_evidence_and_has_no_human_confirmation():
    with pytest.raises(ValidationError):
        FieldValue(state="observed")
    value = FieldValue(state="observed", values=("synthetic",))
    suggestion = TagSuggestion(suggestion_id="synthetic", field="synthetic_field", value=value,
                               origin="ai", producer_version="synthetic-v1", evidence_ids=("synthetic-e",))
    with pytest.raises(ValidationError):
        suggestion.origin = "tool"
    data = json.loads(suggestion.model_dump_json())
    data["confirmed_at"] = "2026-11-07T00:00:00Z"
    with pytest.raises(ValidationError):
        TagSuggestion.model_validate_json(json.dumps(data))
    data.pop("confirmed_at")
    data["evidence_ids"] = []
    with pytest.raises(ValidationError):
        TagSuggestion.model_validate_json(json.dumps(data))


def test_effective_value_requires_actor_revision_timezone_and_evidence():
    base = {"field": "synthetic_field", "value": {"state": "observed", "values": ["synthetic"]},
            "actor_id": "synthetic-reviewer", "revision": 1, "evidence_ids": ["synthetic-e"],
            "confirmed_at": "2026-11-07T00:00:00Z"}
    assert EffectiveValue.model_validate_json(json.dumps(base)).revision == 1
    for change in ({"actor_id": ""}, {"revision": 0}, {"evidence_ids": []},
                   {"confirmed_at": "2026-11-07T00:00:00"}):
        with pytest.raises(ValidationError):
            EffectiveValue.model_validate_json(json.dumps(base | change))
    bad_result = result_data() | {"effective_values": [base]}
    with pytest.raises(ValidationError):
        WorkerResult.model_validate_json(json.dumps(bad_result))


@pytest.mark.parametrize("path", ["../source.mp4", "D:/source.mp4", "/source.mp4", "a/../b", "a//b", "https://x/a", "a\\b", "a\x00b", "a/CON.mp4", "a/file.", "a/file "])
def test_artifact_references_reject_paths_and_urls(path):
    with pytest.raises(ValidationError):
        ArtifactRef(kind="synthetic", ref=path, sha256=HASH)


def test_segment_requires_interval_and_frames_keep_timestamp():
    base = {"evidence_id": "synthetic-e", "source_sha256": HASH,
            "artifact": {"kind": "synthetic", "ref": "inputs/synthetic.bin", "sha256": HASH}}
    for change in ({"kind": "video_segment"}, {"kind": "frame"},
                   {"kind": "video_segment", "start_seconds": 3.0, "end_seconds": 2.0}):
        with pytest.raises(ValidationError):
            Evidence.model_validate_json(json.dumps(base | change))
    evidence = Evidence.model_validate_json(json.dumps(base | {"kind": "video_segment", "start_seconds": 1.0, "end_seconds": 2.0}))
    assert evidence.end_seconds == 2.0


@pytest.mark.parametrize("change", [{"attempt": 4}, {"attempt": True}, {"lease_epoch": 0},
                                    {"output_prefix": "../originals/"}, {"shell": "anything"},
                                    {"versions": {"schema_version": "future", "rules_version": "v", "preprocess_version": "v"}}])
def test_task_rejects_invalid_budget_lease_path_extra_fields_and_schema(change):
    with pytest.raises(ValidationError):
        TaskEnvelope.model_validate_json(json.dumps(task_data() | change))


def test_result_roundtrip_and_stale_result_rejected():
    task = TaskEnvelope.model_validate_json(json.dumps(task_data()))
    result = WorkerResult.model_validate_json(json.dumps(result_data()))
    validate_result_for_task(task, result)
    assert TaskEnvelope.model_validate_json(task.model_dump_json()) == task
    for change in ({"lease_epoch": 1}, {"attempt": 2}, {"input_fingerprint": "b" * 64},
                   {"versions": {"rules_version": "changed", "preprocess_version": "synthetic-v1"}},
                   {"artifacts": [{"kind": "synthetic", "ref": "other/output.bin", "sha256": HASH}]}):
        stale = WorkerResult.model_validate_json(json.dumps(result_data() | change))
        with pytest.raises(ValueError):
            validate_result_for_task(task, stale)


@pytest.mark.parametrize("change", [{"status": "failed"}, {"status": "partial"}, {"status": "not_applicable"},
                                    {"errors": [{"code": "synthetic-error", "message": "synthetic"}]},
                                    {"elapsed_seconds": float("inf")}])
def test_result_requires_honest_status_and_finite_metrics(change):
    with pytest.raises(ValidationError):
        WorkerResult.model_validate_json(json.dumps(result_data() | change))


def test_result_checks_evidence_links_and_target_fields():
    suggestion = {"suggestion_id": "synthetic-s", "field": "synthetic_field",
                  "value": {"state": "observed", "values": ["synthetic"]}, "origin": "ai",
                  "producer_version": "synthetic-v1", "evidence_ids": ["synthetic-e"]}
    with pytest.raises(ValidationError):
        WorkerResult.model_validate_json(json.dumps(result_data() | {"suggestions": [suggestion]}))
    evidence = {"evidence_id": "synthetic-e", "kind": "image", "source_sha256": HASH,
                "artifact": task_data()["input_refs"][0]}
    result = WorkerResult.model_validate_json(json.dumps(result_data() | {"evidence": [evidence], "suggestions": [suggestion]}))
    task = TaskEnvelope.model_validate_json(json.dumps(task_data()))
    validate_result_for_task(task, result)
    other = suggestion | {"field": "unrequested"}
    result = WorkerResult.model_validate_json(json.dumps(result_data() | {"evidence": [evidence], "suggestions": [other]}))
    with pytest.raises(ValueError, match="unrequested"):
        validate_result_for_task(task, result)
