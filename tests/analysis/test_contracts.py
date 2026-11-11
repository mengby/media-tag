import json

import pytest

from media_tag.analysis.contracts import (
    AnalysisDocument, CleaningPolicy, FieldAssessment, ProviderCapabilities, TOPIC_FIELDS,
)


def missing(field, *, state="unknown", next_step="local", **extra):
    return {"field": field, "value": {"state": state, "reason": "synthetic assessment"},
            "next_step": next_step, "reason": "synthetic routing", **extra}


def document(kind="image"):
    return {"analysis_id": "synthetic-analysis", "asset_id": "synthetic-asset", "asset_revision": 1,
            "content_sha256": "a" * 64, "content_kind": kind,
            "fields": [missing(field) for field in TOPIC_FIELDS]}


def evidence(kind="image", source="a" * 64):
    item = {"evidence_id": "sample", "kind": kind, "source_sha256": source,
            "artifact": {"kind": "sample", "ref": "jobs/synthetic/sample.bin", "sha256": "b" * 64}}
    if kind in ("frame", "video_segment", "audio_segment"):
        item["start_seconds"] = 0.0
    if kind in ("video_segment", "audio_segment"):
        item["end_seconds"] = 4.0
    return item


def observed(field="画面主体", text="synthetic subject", **extra):
    return {"field": field, "value": {"state": "observed", "values": [text]},
            "next_step": "none", "reason": "synthetic evidence", "origin": "local",
            "versions": {"rules_version": "synthetic-rules", "preprocess_version": "synthetic-input"},
            "evidence_ids": ["sample"], **extra}


def parse(value):
    return AnalysisDocument.model_validate_json(json.dumps(value))


def replace(value, assessment):
    value["fields"][TOPIC_FIELDS.index(assessment["field"])] = assessment


def test_partial_local_results_do_not_claim_complete_or_approved():
    value = document()
    value["evidence"] = [evidence()]
    replace(value, observed())
    result = parse(value)
    assert not result.analysis_complete
    assert not result.medical_confirmation and not result.privacy_confirmation
    assert not result.effective_values_written
    assert next(f for f in result.fields if f.field == "科室").requires_field_review


@pytest.mark.parametrize("field", ["medical_confirmation", "privacy_confirmation", "effective_values_written"])
def test_analysis_cannot_claim_human_approval(field):
    with pytest.raises(ValueError):
        parse(document() | {field: True})


@pytest.mark.parametrize("state", ["unknown", "no_match", "failed"])
def test_missing_model_or_unresolved_value_cannot_be_disguised_as_finished(state):
    with pytest.raises(ValueError):
        FieldAssessment.model_validate_json(json.dumps(missing("科室", state=state, next_step="none")))


def test_not_applicable_is_not_pending_provider():
    with pytest.raises(ValueError):
        FieldAssessment.model_validate_json(json.dumps(missing("画面主体", state="not_applicable", next_step="provider")))


@pytest.mark.parametrize("alter", ["missing", "duplicate", "unexpected"])
def test_all_ten_original_fields_are_required(alter):
    value = document()
    if alter == "missing":
        value["fields"].pop()
    elif alter == "duplicate":
        value["fields"][-1] = value["fields"][0]
    else:
        value["fields"][-1] = missing("new unofficial field")
    with pytest.raises(ValueError):
        parse(value)


@pytest.mark.parametrize("kind", ["image", "frame", "filename", "human_record"])
def test_pure_audio_cannot_be_labeled_from_cover_or_filename(kind):
    value = document("audio")
    for field in ("画面主体", "表现形式"):
        replace(value, missing(field, state="not_applicable", next_step="none"))
    value["evidence"] = [evidence(kind)]
    replace(value, observed("中文检索词", "doorbell"))
    with pytest.raises(ValueError):
        parse(value)


def test_audio_evidence_and_visual_not_applicability_still_retain_other_pending_fields():
    value = document("audio")
    for field in ("画面主体", "表现形式"):
        replace(value, missing(field, state="not_applicable", next_step="none"))
    value["evidence"] = [evidence("audio_segment")]
    replace(value, observed("中文检索词", "doorbell"))
    assert not parse(value).analysis_complete


@pytest.mark.parametrize("fault", ["source", "reference", "producer"])
def test_observed_fields_require_bound_source_evidence_and_producer(fault):
    value = document()
    value["evidence"] = [evidence(source="c" * 64 if fault == "source" else "a" * 64)]
    item = observed()
    if fault == "reference":
        item["evidence_ids"] = ["missing-evidence"]
    elif fault == "producer":
        item["origin"] = None
    replace(value, item)
    with pytest.raises(ValueError):
        parse(value)


def test_dynamic_claim_requires_actual_continuous_video_evidence():
    value = document("video")
    value["evidence"] = [evidence("frame")]
    replace(value, observed("过程/状态", "small→large"))
    with pytest.raises(ValueError):
        parse(value)
    value["evidence"] = [evidence("video_segment")]
    assert parse(value).fields[5].requires_field_review


def test_no_match_retains_unpublished_candidate_without_becoming_a_formal_value():
    value = document()
    value["evidence"] = [evidence()]
    replace(value, missing("疾病/症状", state="no_match", next_step="vocabulary",
                           candidates=["synthetic-unpublished-candidate"], origin="local",
                           versions={"rules_version": "synthetic", "preprocess_version": "synthetic"},
                           evidence_ids=["sample"]))
    item = parse(value).fields[3]
    assert item.value.values is None and item.candidates
    assert not parse(value).analysis_complete


def test_unmapped_candidate_still_requires_a_producer_and_media_evidence():
    value = document()
    replace(value, missing("疾病/症状", state="no_match", next_step="vocabulary", candidates=["unsupported"]))
    with pytest.raises(ValueError):
        parse(value)


def test_audio_track_cannot_establish_a_visual_subject():
    value = document("video")
    value["evidence"] = [evidence("audio_segment")]
    replace(value, observed())
    with pytest.raises(ValueError):
        parse(value)


def test_static_image_cannot_claim_audio_content():
    value = document("image")
    value["evidence"] = [evidence("audio_segment")]
    replace(value, observed("中文检索词", "music"))
    with pytest.raises(ValueError):
        parse(value)


def test_complete_ai_assessment_is_separate_from_any_human_approval():
    value = document()
    value["evidence"] = [evidence()]
    for field in TOPIC_FIELDS:
        replace(value, missing(field, state="not_applicable", next_step="none")
                if field in ("科室", "器官/系统", "疾病/症状", "过程/状态", "情绪/语气")
                else observed(field))
    result = parse(value)
    assert result.analysis_complete
    assert not result.medical_confirmation and not result.privacy_confirmation


def test_reuse_requires_existing_producer_and_does_not_approve_medical_field():
    item = observed("科室", "synthetic", origin="reused")
    with pytest.raises(ValueError):
        FieldAssessment.model_validate_json(json.dumps(item))
    result = FieldAssessment.model_validate_json(json.dumps(item | {"reused_from": "existing-run"}))
    assert result.requires_field_review


def test_proposed_cleaning_rules_preserve_existing_defaults():
    policy = CleaningPolicy(policy_version="ct-proposed-v1")
    assert policy.video_duration.status == "proposed"
    assert policy.video_duration.default_speed == 1.0
    assert policy.video_duration.applies_to == "video"
    assert policy.audio_policy == policy.face_policy == policy.watermark_policy == "unselected"
    with pytest.raises(ValueError):
        CleaningPolicy(policy_version="bad", preserve_originals=False)


def test_visual_provider_does_not_implicitly_support_audio():
    result = ProviderCapabilities.model_validate_json(json.dumps({
        "provider_id": "synthetic", "adapter_version": "v1", "model_id": "not-an-actual-call",
        "inputs": ["image", "video"], "execution": "local"}))
    assert "audio" not in result.inputs


def producer_document():
    value = document()
    value['evidence'] = [evidence()]
    assessment = observed(producer_ids=['synthetic-producer'])
    replace(value, assessment)
    value['producers'] = [{'producer_id': 'synthetic-producer', 'task_id': 'synthetic-task',
        'stage': 'visual_fields', 'lease_epoch': 1, 'source_sha256': value['content_sha256'],
        'input_fingerprint': 'c'*64, 'result_sha256': 'd'*64,
        'report': {'kind': 'visual_fields_report', 'ref': 'jobs/synthetic/report.json', 'sha256': 'e'*64},
        'versions': assessment['versions'], 'evidence_ids': ['sample']}]
    return value


def test_runtime_provenance_binds_source_versions_and_used_evidence():
    value = producer_document()
    parsed = parse(value)
    assert parsed.producers[0].task_id == 'synthetic-task'
    assert not parsed.analysis_complete and not parsed.effective_values_written


@pytest.mark.parametrize('mutation', ['source', 'versions', 'evidence', 'producer_id', 'duplicate'])
def test_runtime_provenance_cannot_point_to_an_unrelated_producer(mutation):
    value = producer_document()
    if mutation == 'source':
        value['producers'][0]['source_sha256'] = 'f'*64
    elif mutation == 'versions':
        value['producers'][0]['versions'] = {'rules_version': 'other', 'preprocess_version': 'other'}
    elif mutation == 'evidence':
        value['producers'][0]['evidence_ids'] = []
    elif mutation == 'producer_id':
        value['producers'][0]['producer_id'] = 'other'
    else:
        value['producers'].append(value['producers'][0])
    with pytest.raises(ValueError):
        parse(value)
