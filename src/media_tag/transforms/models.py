from typing import Annotated, Literal
from pydantic import Field, model_validator
from media_tag.contracts.models import ContractModel, Identifier, Sha256
from media_tag.variants.models import SourceSegment

RENDER_VERSION = 'd11-redaction-retime-v1'
TRACK_VERSION = 'd10-face-track-v1'
RETIME_VERSION = 'd11-retime-v1'


class RedactionDeployment(ContractModel):
    enabled: bool = False
    reviewers: tuple[Identifier, ...] = ()


class RetimeDeployment(RedactionDeployment):
    pass


class VideoOptions(ContractModel):
    max_frames: Annotated[int, Field(ge=1, le=7200)] = 1800
    max_seconds: Annotated[float, Field(gt=0, le=600)] = 60.0
    max_pixels: Annotated[int, Field(ge=1, le=4096**2)] = 1920**2
    max_total_pixels: Annotated[int, Field(ge=1, le=8_000_000_000)] = 1_000_000_000


class ScanOptions(VideoOptions):
    score_threshold: Annotated[float, Field(gt=0, le=1)] = .6
    cut_threshold: Annotated[float, Field(gt=0, le=1)] = .30
    max_gap_seconds: Annotated[float, Field(ge=0, le=.5)] = .15


class ScanSubmission(ContractModel):
    request_id: Identifier
    parent_asset_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    options: ScanOptions = ScanOptions()
    chain_from_derived: bool = False


class ScanTask(ScanOptions):
    variant_plan_id: Identifier
    variant_plan_hash: Sha256


Box = Annotated[tuple[float, float, float, float], Field(min_length=4, max_length=4)]


class Keyframe(ContractModel):
    seconds: Annotated[float, Field(ge=0)]
    box: Box

    @model_validator(mode='after')
    def valid_box(self):
        x1,y1,x2,y2 = self.box
        if not 0 <= x1 < x2 <= 1 or not 0 <= y1 < y2 <= 1:
            raise ValueError('normalized display coordinates required')
        return self


class TrackEdit(ContractModel):
    action: Literal['exclude', 'trim', 'split', 'merge', 'manual']
    track_ids: Annotated[tuple[Identifier, ...], Field(max_length=64)] = ()
    new_track_id: Identifier | None = None
    start_seconds: Annotated[float, Field(ge=0)] | None = None
    end_seconds: Annotated[float, Field(gt=0)] | None = None
    keyframes: Annotated[tuple[Keyframe, ...], Field(max_length=100)] = ()


class RevisionSubmission(ContractModel):
    scan_task_id: Identifier
    lease_epoch: Annotated[int, Field(ge=1)]
    report_sha256: Sha256
    edits: Annotated[tuple[TrackEdit, ...], Field(max_length=200)] = ()


class RenderOptions(ContractModel):
    variant_plan_id: Identifier
    variant_plan_hash: Sha256
    revision_hash: Sha256


class RenderSubmission(ContractModel):
    request_id: Identifier
    revision_hash: Sha256
    mode: Literal['mosaic', 'solid'] = 'mosaic'
    strength: Annotated[int, Field(ge=1, le=100)] = 20
    padding: Annotated[float, Field(ge=0, le=1)] = .25
    audio: Literal['preserve_pitch', 'drop'] = 'preserve_pitch'
    audio_stream_index: Annotated[int, Field(ge=0)] | None = None
    chain_from_derived: bool = False
    speed: Annotated[float, Field(ge=.25, le=4)] = 1.0
    segments: Annotated[tuple[SourceSegment, ...], Field(max_length=100)] = ()


class RetimeSubmission(ContractModel):
    request_id: Identifier
    parent_asset_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    speed: Annotated[float, Field(ge=.25, le=4)] = 1.0
    audio: Literal['preserve_pitch', 'drop'] = 'preserve_pitch'
    audio_stream_index: Annotated[int, Field(ge=0)] | None = None
    chain_from_derived: bool = False
    segments: Annotated[tuple[SourceSegment, ...], Field(max_length=100)] | None = None
    options: VideoOptions = VideoOptions()


class RetimeTask(VideoOptions):
    variant_plan_id: Identifier
    variant_plan_hash: Sha256


class ReviewSubmission(ContractModel):
    expected_revision: Annotated[int, Field(ge=1)]
    output_sha256: Sha256
    recipe_hash: Sha256
    decision: Literal['approved', 'revoked']
    full_output_viewed: bool
    exclusions_checked: bool
    risk_intervals_checked: bool
    note: Annotated[str, Field(min_length=1, max_length=1000)]
