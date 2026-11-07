from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.contracts.models import ContractModel, Identifier


class MaskSpec(ContractModel):
    revision: Identifier
    mode: Literal['mosaic', 'solid'] = 'mosaic'
    strength: Annotated[int, Field(ge=1, le=100)] = 20
    padding: Annotated[float, Field(ge=0, le=1)] = .25


class Recipe(ContractModel):
    schema_version: Literal['d09-v1'] = 'd09-v1'
    speed: Annotated[float, Field(ge=0.25, le=4)] = 1.0
    audio: Literal['preserve_pitch', 'drop'] = 'preserve_pitch'
    audio_stream_index: Annotated[int, Field(ge=0)] | None = None
    mask: MaskSpec | None = None
    codec: Literal['h264'] = 'h264'
    container: Literal['mp4'] = 'mp4'
    crf: Annotated[int, Field(ge=18, le=28)] = 23
    preset: Literal['veryfast', 'medium'] = 'medium'
    renderer_revision: Identifier | None = None
    model_revision: Identifier | None = None

    @model_validator(mode='after')
    def check_audio(self):
        if self.audio == 'drop' and self.audio_stream_index is not None:
            raise ValueError('drop audio cannot select an output track')
        return self


class PlanSpec(ContractModel):
    parent_asset_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    recipe: Recipe = Field(default_factory=Recipe)
    chain_from_derived: bool = False


class SourceClock(ContractModel):
    origin_seconds: float
    duration_seconds: Annotated[float, Field(gt=0)]
    time_base_numerator: Annotated[int, Field(ge=1)]
    time_base_denominator: Annotated[int, Field(ge=1)]


class FixedTimeMap(ContractModel):
    schema_version: Literal['d09-fixed-v1'] = 'd09-fixed-v1'
    clock: SourceClock
    speed: Annotated[float, Field(ge=0.25, le=4)] = 1.0

    def source_to_output(self, seconds: float) -> float:
        if not 0 <= seconds <= self.clock.duration_seconds:
            raise ValueError('normalized source position is out of bounds')
        return seconds / self.speed

    @property
    def expected_duration(self):
        return self.clock.duration_seconds / self.speed
