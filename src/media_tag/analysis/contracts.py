"""All-media analysis contracts, separate from human effective values and approvals."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.contracts.models import (
    ContractModel, Evidence, FieldValue, Identifier, PositiveInt, Sha256, Text, Versions,
)

TopicField = Literal[
    "用途大类", "科室", "器官/系统", "疾病/症状", "画面主体", "过程/状态",
    "表现形式", "中文检索词", "英文检索词", "情绪/语气",
]
TOPIC_FIELDS = (
    "用途大类", "科室", "器官/系统", "疾病/症状", "画面主体", "过程/状态",
    "表现形式", "中文检索词", "英文检索词", "情绪/语气",
)
MEDICAL_FIELDS = frozenset(("科室", "器官/系统", "疾病/症状", "过程/状态"))
ContentKind = Literal["image", "video", "animation", "audio"]
NextStep = Literal[
    "none", "local", "precision", "evidence", "vocabulary", "provider", "human",
]
Candidate = Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]


class FieldAssessment(ContractModel):
    """An AI/rule assessment, never an EffectiveValue or an approval."""

    field: TopicField
    value: FieldValue
    next_step: NextStep
    reason: Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]
    origin: Literal["local", "precision", "reused"] | None = None
    versions: Versions | None = None
    evidence_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)] = ()
    candidates: Annotated[tuple[Candidate, ...], Field(max_length=16)] = ()
    reused_from: Identifier | None = None
    dynamic: bool = False

    @model_validator(mode="after")
    def check_assessment(self):
        if len(set(self.evidence_ids)) != len(self.evidence_ids):
            raise ValueError("duplicate assessment evidence")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("duplicate unmapped candidates")
        if self.value.state == "observed" or self.candidates:
            if self.origin is None or self.versions is None or not self.evidence_ids:
                raise ValueError("observations and raw candidates require a producer and evidence")
        if self.value.state in ("unknown", "no_match", "failed") and self.next_step == "none":
            raise ValueError("unresolved fields must retain an explicit next step")
        if self.value.state == "not_applicable" and self.next_step != "none":
            raise ValueError("not-applicable is not a substitute for a pending capability")
        if (self.origin == "reused") != (self.reused_from is not None):
            raise ValueError("only reused assessments require an existing producer reference")
        return self

    @property
    def requires_field_review(self) -> bool:
        return self.field in MEDICAL_FIELDS


class AnalysisDocument(ContractModel):
    schema_version: Literal["ct-topic-analysis-v1"] = "ct-topic-analysis-v1"
    analysis_id: Identifier
    asset_id: Identifier
    asset_revision: PositiveInt
    content_sha256: Sha256
    content_kind: ContentKind
    fields: Annotated[tuple[FieldAssessment, ...], Field(min_length=10, max_length=10)]
    evidence: Annotated[tuple[Evidence, ...], Field(max_length=64)] = ()
    medical_confirmation: Literal[False] = False
    privacy_confirmation: Literal[False] = False
    effective_values_written: Literal[False] = False

    @model_validator(mode="after")
    def check_binding(self):
        names = [item.field for item in self.fields]
        if len(set(names)) != 10 or set(names) != set(TOPIC_FIELDS):
            raise ValueError("exactly the ten original topic fields are required")
        ids = [item.evidence_id for item in self.evidence]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate document evidence")
        if any(item.source_sha256 != self.content_sha256 for item in self.evidence):
            raise ValueError("evidence must refer to the actual analyzed file")
        by_id = {item.evidence_id: item for item in self.evidence}
        for item in self.fields:
            if any(key not in by_id for key in item.evidence_ids):
                raise ValueError("assessment references missing evidence")
            evidence = [by_id[key] for key in item.evidence_ids]
            if item.value.state == "observed" or item.candidates:
                if any(e.kind in ("filename", "human_record") for e in evidence):
                    raise ValueError("AI content assessments require actual media evidence")
                if self.content_kind == "audio" and not any(e.kind == "audio_segment" for e in evidence):
                    raise ValueError("audio labels require actual audio segment evidence")
                if self.content_kind != "audio" and not any(
                    e.kind in ("image", "frame", "video_segment", "audio_segment") for e in evidence
                ):
                    raise ValueError("OCR alone is not a semantic topic assessment")
                if self.content_kind == "image" and not any(e.kind == "image" for e in evidence):
                    raise ValueError("image labels require actual image evidence")
                if item.field in ("画面主体", "表现形式") and not any(
                    e.kind in ("image", "frame", "video_segment") for e in evidence
                ):
                    raise ValueError("visual fields require visual evidence")
            directional = item.field == "过程/状态" and any(
                "→" in value or "->" in value for value in (*(item.value.values or ()), *item.candidates)
            )
            if (item.value.state == "observed" or item.candidates) and (item.dynamic or directional):
                if not any(e.kind == "video_segment" for e in evidence):
                    raise ValueError("dynamic visual claims require continuous video evidence")
            if self.content_kind == "audio" and item.field in ("画面主体", "表现形式"):
                if item.value.state != "not_applicable":
                    raise ValueError("visual fields are not applicable to pure audio")
        return self

    @property
    def analysis_complete(self) -> bool:
        """Does not imply human confirmation, privacy approval or publishability."""
        return all(item.next_step == "none" for item in self.fields)


class VideoDurationPolicy(ContractModel):
    """A proposed preset is never permission to transform an input."""

    status: Literal["proposed", "approved"] = "proposed"
    mode: Literal["segment_preferred", "whole_input"] = "segment_preferred"
    minimum_seconds: Annotated[float, Field(gt=0, le=60)] = 3.0
    maximum_seconds: Annotated[float, Field(gt=0, le=60)] = 5.0
    default_speed: Literal[1.0] = 1.0
    applies_to: Literal["video"] = "video"

    @model_validator(mode="after")
    def check_interval(self):
        if self.minimum_seconds > self.maximum_seconds:
            raise ValueError("duration bounds must be ordered")
        return self


class CleaningPolicy(ContractModel):
    schema_version: Literal["ct-cleaning-policy-v1"] = "ct-cleaning-policy-v1"
    policy_version: Text
    video_duration: VideoDurationPolicy = VideoDurationPolicy()
    audio_policy: Literal["unselected", "preserve_pitch", "remove"] = "unselected"
    preserve_originals: Literal[True] = True
    preserve_transparency: Literal[True] = True
    preserve_animation_cycles: Literal[True] = True
    face_policy: Literal["unselected", "all_real_faces"] = "unselected"
    watermark_policy: Literal["unselected", "manual_regions"] = "unselected"


class ProviderCapabilities(ContractModel):
    """Declared capabilities; actual model acceptance must be verified separately."""

    provider_id: Identifier
    adapter_version: Text
    model_id: Text
    inputs: Annotated[
        tuple[Literal["image", "multi_image", "video", "audio"], ...],
        Field(min_length=1, max_length=4),
    ]
    execution: Literal["local", "external"]

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError("duplicate provider input capabilities")
        return self
