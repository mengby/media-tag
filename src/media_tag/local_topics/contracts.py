from typing import Annotated, Literal

from pydantic import Field

from media_tag.analysis.contracts import AnalysisDocument
from media_tag.contracts.models import ArtifactRef, ContractModel, Sha256, Versions, Identifier


class Options(ContractModel):
    max_input_bytes: Annotated[int, Field(ge=1, le=64*1024**2)] = 32*1024**2
    max_source_pixels: Annotated[int, Field(ge=1, le=16*1024**2)] = 16*1024**2
    max_pixels: Annotated[int, Field(ge=128**2, le=768**2)] = 512**2
    max_new_tokens: Annotated[int, Field(ge=64, le=384)] = 192
    max_options_characters: Annotated[int, Field(ge=256, le=12000)] = 4000


class Generation(ContractModel):
    """A backend response; service still validates every answer and its bindings."""
    backend: Literal['local_qwen', 'synthetic_protocol']
    model_revision: Sha256
    prompt_version: str
    image_sha256: Sha256
    answers: Annotated[tuple[Annotated[str, Field(max_length=16000)], ...], Field(min_length=10, max_length=10)]


class LocalTopicResult(ContractModel):
    schema_version: Literal['ct-local-topics-v1'] = 'ct-local-topics-v1'
    document: AnalysisDocument
    raw_report: ArtifactRef
    image: ArtifactRef
    prompt_version: str
    policy_digest: Sha256
    model_revision: Sha256
    upstream_model: Literal['Qwen/Qwen3-VL-2B-Instruct'] = 'Qwen/Qwen3-VL-2B-Instruct'
    backend: Literal['local_qwen', 'synthetic_protocol']
    draft_attempt_complete: Literal[True] = True
    calibration_status: Literal['not_calibrated'] = 'not_calibrated'
    precision_exemption: Literal[False] = False
    medical_confirmation: Literal[False] = False
    privacy_confirmation: Literal[False] = False
    effective_values_written: Literal[False] = False


class LocalTopicDeployment(ContractModel):
    enabled: bool = False
    model_root: str = 'cache/models/semantic'


class LocalTopicOptions(Options):
    model_revision: Sha256
    policy_digest: Sha256


class RouterTopicReport(ContractModel):
    schema_version: Literal['ct-local-topics-router-v1'] = 'ct-local-topics-router-v1'
    task_id: Identifier
    lease_epoch: Annotated[int, Field(ge=1)]
    input_fingerprint: Sha256
    versions: Versions
    source: ArtifactRef
    native_input: ArtifactRef
    result: LocalTopicResult
