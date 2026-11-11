"""Descriptors identify the exact source version and preprocessing recipe."""
from hashlib import sha256
import math
from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.contracts.models import ContractModel, Identifier, PositiveInt, Sha256, Text

VISUAL_VERSION = 'ct-visual-dhash-alpha-v2'
AUDIO_VERSION = 'ct-audio-logbands-v2'
CLIP_VERSION = 'ct-clip-cosine-v1'


class SourceVersion(ContractModel):
    asset_id: Identifier
    asset_revision: PositiveInt
    content_sha256: Sha256


class VisualFrame(ContractModel):
    dhash: Annotated[str, Field(pattern='^[0-9a-f]{64}$')]
    alpha_hash: Annotated[str, Field(pattern='^[0-9a-f]{64}$')]
    mean_rgb: Annotated[tuple[float, ...], Field(min_length=3, max_length=3)]
    luma_grid: Annotated[tuple[float, ...], Field(min_length=64, max_length=64)]
    normalized_seconds: Annotated[float, Field(ge=0)] | None = None

    @model_validator(mode='after')
    def bounds(self):
        if any(not 0 <= value <= 255 for value in (*self.mean_rgb, *self.luma_grid)):
            raise ValueError('decoded visual values must remain in byte range')
        return self


class VisualFingerprint(ContractModel):
    schema_version: Literal['ct-visual-fingerprint-v2'] = 'ct-visual-fingerprint-v2'
    algorithm_version: Literal['ct-visual-dhash-alpha-v2'] = VISUAL_VERSION
    source: SourceVersion
    media_kind: Literal['image', 'animation', 'video']
    frames: Annotated[tuple[VisualFrame, ...], Field(min_length=1, max_length=96)]
    coverage: Literal['complete_image', 'complete_animation_frames', 'sparse_decoded_video_frames']
    frame_durations_ms: tuple[float | None, ...] = ()
    source_origin_seconds: float | None = None
    decoder_identity: str
    full_video_observed: Literal[False] = False

    @model_validator(mode='after')
    def binding(self):
        if self.media_kind == 'image' and (len(self.frames) != 1 or self.coverage != 'complete_image'):
            raise ValueError('one actual still-image frame required')
        if self.media_kind == 'animation' and len(self.frames) != len(self.frame_durations_ms):
            raise ValueError('animation frame timing must retain one entry per actual frame')
        if any(value is not None and value < 0 for value in self.frame_durations_ms):
            raise ValueError('negative animation timing')
        if self.media_kind == 'video' and (self.coverage != 'sparse_decoded_video_frames' or any(f.normalized_seconds is None for f in self.frames)):
            raise ValueError('video candidates require actual timed decoded samples')
        return self

    @property
    def digest(self):
        return sha256(self.model_dump_json().encode()).hexdigest()


class AudioFingerprint(ContractModel):
    schema_version: Literal['ct-audio-fingerprint-v2'] = 'ct-audio-fingerprint-v2'
    algorithm_version: Literal['ct-audio-logbands-v2'] = AUDIO_VERSION
    source: SourceVersion
    sample_rate: Literal[16000] = 16000
    source_stream_index: Annotated[int, Field(ge=0)]
    source_origin_seconds: float | None = None
    decoded_duration_seconds: Annotated[float, Field(gt=0, le=120)]
    decoded_sample_count: Annotated[int, Field(strict=True, ge=1, le=120*16000)]
    decoded_clock: Literal['pcm_samples_from_selected_stream_start'] = 'pcm_samples_from_selected_stream_start'
    pts_normalization: Literal['asetpts=PTS-STARTPTS'] = 'asetpts=PTS-STARTPTS'
    feature_step_seconds: Annotated[float, Field(gt=0)]
    spectral_frames: Annotated[tuple[tuple[float, ...], ...], Field(min_length=1, max_length=512)]
    active_fraction: Annotated[float, Field(ge=0, le=1)]
    channels_strategy: Literal['ffmpeg_mono_mix'] = 'ffmpeg_mono_mix'
    coverage: Literal['complete_selected_audio_stream'] = 'complete_selected_audio_stream'
    decoder_identity: str

    @model_validator(mode='after')
    def vectors(self):
        if abs(self.decoded_duration_seconds-self.decoded_sample_count/16000.0) > .5/16000.0:
            raise ValueError('decoded duration must agree with actual PCM sample count')
        if any(len(frame) != 24 or any(not 0 <= value <= 1 for value in frame) for frame in self.spectral_frames):
            raise ValueError('24 finite normalized spectral bands required')
        return self

    @property
    def digest(self):
        return sha256(self.model_dump_json().encode()).hexdigest()


class Candidate(ContractModel):
    schema_version: Literal['ct-similarity-candidate-v1'] = 'ct-similarity-candidate-v1'
    left: SourceVersion
    right: SourceVersion
    left_descriptor_sha256: Sha256
    right_descriptor_sha256: Sha256
    algorithm_version: Literal['ct-visual-dhash-alpha-v1', 'ct-visual-dhash-alpha-v2',
        'ct-audio-logbands-v1', 'ct-audio-logbands-v2', 'ct-clip-cosine-v1']
    comparison: Literal['exact_bytes', 'visual_overlap', 'audio_overlap']
    score: Annotated[float, Field(ge=0, le=1)]
    score_kind: Literal['uncalibrated_similarity_not_probability'] = 'uncalibrated_similarity_not_probability'
    estimated_right_offset_seconds: float | None = None
    overlap_seconds: Annotated[float, Field(gt=0)] | None = None
    temporal_alignment_verified: Literal[False] = False
    calibrated: Literal[False] = False
    duplicate_confirmed: Literal[False] = False
    labels_copied: Literal[False] = False
    ownership_changed: Literal[False] = False


class ClipFingerprint(ContractModel):
    schema_version: Literal['ct-clip-fingerprint-v1'] = 'ct-clip-fingerprint-v1'
    algorithm_version: Literal['ct-clip-cosine-v1'] = CLIP_VERSION
    source: SourceVersion
    model_revision: Text
    preprocess_version: Text
    producer_task_id: Identifier
    producer_lease_epoch: PositiveInt
    producer_report_sha256: Sha256
    frame_sha256: Annotated[tuple[Sha256, ...], Field(min_length=1, max_length=12)]
    vectors: Annotated[tuple[tuple[float, ...], ...], Field(min_length=1, max_length=12)]
    coverage: Literal['verified_current_clip_sample_features'] = 'verified_current_clip_sample_features'
    full_video_observed: Literal[False] = False

    @model_validator(mode='after')
    def check_vectors(self):
        if len(self.frame_sha256) != len(self.vectors) or any(len(v) != 512 or not math.isfinite(math.hypot(*v)) or math.hypot(*v) <= 1e-12 for v in self.vectors):
            raise ValueError('nonzero 512-dimensional vectors bound to actual frames required')
        return self

    @property
    def digest(self):
        return sha256(self.model_dump_json().encode()).hexdigest()


Descriptor = VisualFingerprint | AudioFingerprint | ClipFingerprint
