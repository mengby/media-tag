"""Structural contracts only; database adoption and human permissions are separate."""

from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from . import SCHEMA_VERSION

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
PositiveInt = Annotated[int, Field(strict=True, ge=1)]
RelativePath = Annotated[str, Field(min_length=1)]


def validate_relative_path(value: str) -> str:
    """Use portable relative paths, not shell strings, drives, or URLs."""
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("reference must be a normalized relative path")
    if any(char in value for char in '\\:<>"|?*\x00') or any(ord(c) < 32 for c in value):
        raise ValueError("reference contains forbidden path characters")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(part.endswith((" ", ".")) or part.split(".")[0].upper() in reserved for part in parts):
        raise ValueError("reference contains Windows reserved or aliased path components")
    return value


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, allow_inf_nan=False)


class ArtifactRef(ContractModel):
    kind: Text
    ref: RelativePath
    sha256: Sha256

    @model_validator(mode="after")
    def check_path(self) -> Self:
        validate_relative_path(self.ref)
        return self


class Evidence(ContractModel):
    evidence_id: Identifier
    kind: Literal["image", "frame", "video_segment", "audio_segment", "ocr", "human_record", "filename"]
    source_sha256: Sha256
    artifact: ArtifactRef
    start_seconds: Annotated[float, Field(ge=0)] | None = None
    end_seconds: Annotated[float, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def check_interval(self) -> Self:
        if self.kind in ("video_segment", "audio_segment"):
            if self.start_seconds is None or self.end_seconds is None:
                raise ValueError("segment evidence requires start and end times")
        if self.end_seconds is not None:
            if self.start_seconds is None or self.end_seconds <= self.start_seconds:
                raise ValueError("evidence interval must have positive duration")
        if self.kind == "frame" and self.start_seconds is None:
            raise ValueError("frame evidence requires a source timestamp")
        return self


class FieldValue(ContractModel):
    state: Literal["observed", "unknown", "no_match", "not_applicable", "failed"]
    values: tuple[Text, ...] | None = None
    reason: Text | None = None

    @model_validator(mode="after")
    def check_state(self) -> Self:
        if self.state == "observed":
            if not self.values:
                raise ValueError("observed requires nonempty values")
            if len(set(self.values)) != len(self.values):
                raise ValueError("duplicate field values")
        elif self.values is not None or self.reason is None:
            raise ValueError("non-observed states require reason and null values")
        return self


class TagSuggestion(ContractModel):
    suggestion_id: Identifier
    field: Text
    value: FieldValue
    origin: Literal["ai", "tool"]
    producer_version: Text
    evidence_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def require_evidence(self) -> Self:
        if self.value.state == "observed" and not self.evidence_ids:
            raise ValueError("observed suggestion requires evidence")
        return self


class EffectiveValue(ContractModel):
    """Explicit human revision, never produced by a WorkerResult."""

    field: Text
    value: FieldValue
    actor_id: Identifier
    revision: PositiveInt
    evidence_ids: Annotated[tuple[Identifier, ...], Field(min_length=1)]
    confirmed_at: AwareDatetime


class Versions(ContractModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    rules_version: Text
    preprocess_version: Text
    taxonomy_version: Text | None = None
    lexicon_version: Text | None = None
    model_revision: Text | None = None


class TaskLimits(ContractModel):
    timeout_seconds: PositiveInt = 300
    max_attempts: PositiveInt = 3
    gpu_slots: Literal[0, 1] = 0


class TaskEnvelope(ContractModel):
    task_id: Identifier
    job_id: Identifier
    asset_id: Identifier
    asset_revision: PositiveInt
    stage: Text
    attempt: PositiveInt
    lease_epoch: PositiveInt
    input_fingerprint: Sha256
    input_refs: Annotated[tuple[ArtifactRef, ...], Field(min_length=1)]
    versions: Versions
    target_fields: tuple[Text, ...] = ()
    limits: TaskLimits = Field(default_factory=TaskLimits)
    output_prefix: Text

    @model_validator(mode="after")
    def check_attempt_and_output(self) -> Self:
        if self.attempt > self.limits.max_attempts:
            raise ValueError("attempt exceeds total attempt budget")
        expected = f"jobs/{self.job_id}/runs/{self.task_id}/attempt-{self.attempt}/"
        if self.output_prefix != expected:
            raise ValueError("output_prefix must match job, task and attempt")
        validate_relative_path(self.output_prefix.rstrip("/"))
        if len(set(self.target_fields)) != len(self.target_fields):
            raise ValueError("duplicate target fields")
        return self


class ErrorDetail(ContractModel):
    code: Identifier
    message: Text
    retryable: bool = False


class WorkerResult(ContractModel):
    task_id: Identifier
    attempt: PositiveInt
    lease_epoch: PositiveInt
    input_fingerprint: Sha256
    versions: Versions
    status: Literal["succeeded", "partial", "failed", "not_applicable"]
    artifacts: tuple[ArtifactRef, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    suggestions: tuple[TagSuggestion, ...] = ()
    elapsed_seconds: Annotated[float, Field(ge=0)]
    peak_memory_bytes: Annotated[int, Field(ge=0)] | None = None
    errors: tuple[ErrorDetail, ...] = ()
    reason: Text | None = None

    @model_validator(mode="after")
    def check_outcome(self) -> Self:
        if self.status in ("failed", "partial") and not self.errors:
            raise ValueError("failed/partial results require explicit errors")
        if self.status in ("succeeded", "not_applicable") and self.errors:
            raise ValueError("successful/not-applicable result cannot contain errors")
        if self.status == "not_applicable" and self.reason is None:
            raise ValueError("not_applicable requires a reason")
        if self.status in ("failed", "not_applicable") and self.suggestions:
            raise ValueError("failed/not-applicable results cannot supply suggestions")
        evidence_ids = [item.evidence_id for item in self.evidence]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("duplicate evidence IDs")
        suggestion_ids = [item.suggestion_id for item in self.suggestions]
        if len(set(suggestion_ids)) != len(suggestion_ids):
            raise ValueError("duplicate suggestion IDs")
        for suggestion in self.suggestions:
            if not set(suggestion.evidence_ids).issubset(evidence_ids):
                raise ValueError("suggestion references missing evidence")
        return self


def validate_result_for_task(task: TaskEnvelope, result: WorkerResult) -> None:
    """Match a fixed envelope; live lease/DB/human-value checks belong to D01/D03/D06."""
    for field in ("task_id", "attempt", "lease_epoch", "input_fingerprint", "versions"):
        if getattr(task, field) != getattr(result, field):
            raise ValueError(f"result mismatch: {field}")
    if any(s.field not in task.target_fields for s in result.suggestions):
        raise ValueError("result contains an unrequested target field")
    refs = (*result.artifacts, *(e.artifact for e in result.evidence))
    allowed_inputs = set(task.input_refs)
    for ref in refs:
        if not ref.ref.startswith(task.output_prefix) and ref not in allowed_inputs:
            raise ValueError("result reference is outside inputs and output prefix")
