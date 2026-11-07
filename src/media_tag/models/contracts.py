from typing import Annotated, Literal

from pydantic import Field

from media_tag.contracts.models import ArtifactRef, ContractModel, Identifier, Sha256


class ModelOptions(ContractModel):
    max_frames: Annotated[int, Field(ge=1, le=12)] = 12
    max_pixels: Annotated[int, Field(ge=1, le=2048**2)] = 2048**2


class OCROptions(ModelOptions):
    text_score: Annotated[float, Field(ge=0, le=1)] = 0.5


class FaceOptions(ModelOptions):
    score_threshold: Annotated[float, Field(gt=0, le=1)] = 0.6
    nms_threshold: Annotated[float, Field(gt=0, le=1)] = 0.3
    max_detections: Annotated[int, Field(ge=1, le=1000)] = 100


class CLIPOptions(ModelOptions):
    top_k: Annotated[int, Field(ge=1, le=10)] = 3


class ModelDeployment(ContractModel):
    enabled: bool = False
    model_root: str = 'cache/models/d04'
    clip_device: Literal['cuda', 'cpu'] = 'cuda'


class DiagnosticCandidate(ContractModel):
    concept_id: Identifier
    field: Identifier
    text: Annotated[str, Field(min_length=1, max_length=128)]


class FrameInput(ContractModel):
    artifact: ArtifactRef
    source_sha256: Sha256
    normalized_seconds: Annotated[float, Field(ge=0)] | None = None


class ModelFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
