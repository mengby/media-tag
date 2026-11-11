from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.contracts.models import ContractModel, Sha256


class VideoSegment(ContractModel):
    start_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    end_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    alignment: Literal['exact', 'inside'] = 'exact'

    @model_validator(mode='after')
    def ordered(self):
        if self.end_seconds <= self.start_seconds:
            raise ValueError('source segment must have positive duration')
        return self


class CleaningRecipe(ContractModel):
    schema_version: Literal['ct-cleaning-recipe-v1'] = 'ct-cleaning-recipe-v1'
    source_sha256: Sha256
    content_kind: Literal['image', 'video', 'animation', 'audio']
    segment: VideoSegment | None = None
    speed: Annotated[float, Field(ge=.25, le=4.0, allow_inf_nan=False)] = 1.0
    target_duration_seconds: Annotated[float, Field(ge=3.0, le=5.0, allow_inf_nan=False)] | None = None
    video_stream_index: Annotated[int, Field(ge=0)] | None = None
    audio_stream_index: Annotated[int, Field(ge=0)] | None = None
    audio_policy: Literal['preserve_pitch', 'remove'] | None = None
    crf: Annotated[int, Field(ge=0, le=35)] = 18
    preset: Literal['ultrafast', 'fast', 'medium'] = 'medium'

    @model_validator(mode='after')
    def check(self):
        if self.content_kind != 'video' and (self.segment is not None or self.speed != 1.0
                or self.target_duration_seconds is not None or self.video_stream_index is not None
                or self.audio_stream_index is not None or self.audio_policy is not None):
            raise ValueError('image, animation and audio copies cannot inherit video transforms')
        if (self.speed != 1.0 or self.target_duration_seconds is not None) and self.segment is None:
            raise ValueError('retiming requires an explicit source segment; never compress an implicit whole file')
        if self.target_duration_seconds is not None and self.speed != 1.0:
            raise ValueError('choose either explicit speed or target duration, not both')
        if self.audio_policy == 'remove' and self.audio_stream_index is not None:
            raise ValueError('removed audio has no selected source track')
        return self


class CleaningLimits(ContractModel):
    max_input_bytes: Annotated[int, Field(ge=1, le=4*1024**3)] = 512*1024**2
    max_output_bytes: Annotated[int, Field(ge=1, le=4*1024**3)] = 512*1024**2
    max_pixels: Annotated[int, Field(ge=1, le=64*1024**2)] = 16*1024**2
    max_frames: Annotated[int, Field(ge=1, le=18000)] = 3000
    max_seconds: Annotated[float, Field(gt=0, le=3600, allow_inf_nan=False)] = 600.0
    max_image_frames: Annotated[int, Field(ge=1, le=512)] = 256
    max_image_pixels_total: Annotated[int, Field(ge=1, le=256*1024**2)] = 64*1024**2
    timeout_seconds: Annotated[float, Field(gt=0, le=600, allow_inf_nan=False)] = 120.0
