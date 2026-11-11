from hashlib import sha256
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.contracts.models import ContractModel, Identifier, Sha256, validate_relative_path

Normalized = Annotated[float, Field(ge=0, le=1)]
Byte = Annotated[int, Field(ge=0, le=255)]


class MaskRegion(ContractModel):
    region_id: Identifier
    purpose: Literal['face', 'watermark', 'other']
    box: tuple[Normalized, Normalized, Normalized, Normalized]
    method: Literal['solid', 'mosaic', 'blur']
    strength: Annotated[int, Field(ge=2, le=128)] = 16
    padding: Annotated[float, Field(ge=0, le=.5)] = 0.0
    color: tuple[Byte, Byte, Byte] = (0, 0, 0)
    frame_start: Annotated[int, Field(ge=0)] = 0
    frame_end: Annotated[int, Field(ge=0)] | None = None

    @model_validator(mode='after')
    def check(self):
        x1, y1, x2, y2 = self.box
        if not x1 < x2 or not y1 < y2:
            raise ValueError('positive normalized display-space region required')
        if self.frame_end is not None and self.frame_end < self.frame_start:
            raise ValueError('ordered inclusive frame interval required')
        return self


class UnmaskedFrameDeclaration(ContractModel):
    """A caller's explicit masking request, never detector evidence or approval."""

    frame_index: Annotated[int, Field(ge=0)]
    source_rgba_sha256: Sha256
    reason: Literal['no_face_mask_requested'] = 'no_face_mask_requested'


class ImageRecipe(ContractModel):
    schema_version: Literal['ct-image-recipe-v1'] = 'ct-image-recipe-v1'
    output_format: Literal['png', 'apng']
    regions: Annotated[tuple[MaskRegion, ...], Field(min_length=1, max_length=128)]
    unmasked_frame_declarations: Annotated[tuple[UnmaskedFrameDeclaration, ...], Field(max_length=1000)] = ()
    coordinates: Literal['normalized_display_after_exif'] = 'normalized_display_after_exif'
    preserve_alpha: Literal[True] = True
    preserve_animation_timing: Literal[True] = True
    watermark_restoration: Literal[False] = False

    @model_validator(mode='after')
    def check(self):
        if len({r.region_id for r in self.regions}) != len(self.regions):
            raise ValueError('unique region identifiers required')
        if len({item.frame_index for item in self.unmasked_frame_declarations}) != len(self.unmasked_frame_declarations):
            raise ValueError('unique unmasked frame declarations required')
        return self


def recipe_fingerprint(renderer_version, recipe):
    return sha256(json.dumps({'renderer': renderer_version, 'recipe': recipe.model_dump(mode='json')},
        sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()).hexdigest()


class ProcessingLimits(ContractModel):
    max_input_bytes: Annotated[int, Field(ge=1, le=512*1024**2)] = 64*1024**2
    max_output_bytes: Annotated[int, Field(ge=1, le=512*1024**2)] = 128*1024**2
    max_pixels: Annotated[int, Field(ge=1, le=64*1024**2)] = 16*1024**2
    max_total_pixels: Annotated[int, Field(ge=1, le=256*1024**2)] = 32*1024**2
    max_frames: Annotated[int, Field(ge=1, le=1000)] = 256


class AppliedRegion(ContractModel):
    region_id: Identifier
    purpose: Literal['face', 'watermark', 'other']
    method: Literal['solid', 'mosaic', 'blur']
    box_xyxy: tuple[int, int, int, int]

    @model_validator(mode='after')
    def check(self):
        x1, y1, x2, y2 = self.box_xyxy
        if not 0 <= x1 < x2 or not 0 <= y1 < y2:
            raise ValueError('positive pixel region required')
        return self


class FrameResult(ContractModel):
    frame_index: Annotated[int, Field(ge=0)]
    role: Literal['still', 'poster', 'animation']
    delay_numerator: Annotated[int, Field(ge=1)] | None = None
    delay_denominator: Annotated[int, Field(ge=1)] | None = None
    alpha_sha256: Sha256
    source_rgba_sha256: Sha256
    rgba_sha256: Sha256
    regions: tuple[AppliedRegion, ...]
    unmasked_frame_declaration: UnmaskedFrameDeclaration | None = None

    @model_validator(mode='after')
    def check(self):
        timed = self.delay_numerator is not None and self.delay_denominator is not None
        if (self.role == 'animation') != timed or self.role != 'animation' and any(
                value is not None for value in (self.delay_numerator, self.delay_denominator)):
            raise ValueError('animation alone requires exact positive delay')
        if self.unmasked_frame_declaration is not None:
            if (self.unmasked_frame_declaration.frame_index != self.frame_index
                    or self.unmasked_frame_declaration.source_rgba_sha256 != self.source_rgba_sha256
                    or any(region.purpose == 'face' for region in self.regions)):
                raise ValueError('unmasked declaration requires this source frame and no face region')
        return self


class InspectedFrame(ContractModel):
    frame_index: Annotated[int, Field(ge=0)]
    role: Literal['still', 'poster', 'animation']
    source_rgba_sha256: Sha256
    alpha_sha256: Sha256
    delay_numerator: Annotated[int, Field(ge=1)] | None = None
    delay_denominator: Annotated[int, Field(ge=1)] | None = None


class SourceInspection(ContractModel):
    schema_version: Literal['ct-image-inspection-v1'] = 'ct-image-inspection-v1'
    renderer_version: str
    source_sha256: Sha256
    source_format: str
    display_width: Annotated[int, Field(ge=1)]
    display_height: Annotated[int, Field(ge=1)]
    source_exif_orientation: Annotated[int, Field(ge=1, le=8)] | None
    frame_count: Annotated[int, Field(ge=1)]
    animation_plays: Annotated[int, Field(ge=0)] | None
    has_default_poster: bool
    frames: tuple[InspectedFrame, ...]
    face_detection_performed: Literal[False] = False
    privacy_confirmation: Literal[False] = False
    quality_confirmation: Literal[False] = False


class ProcessingResult(ContractModel):
    schema_version: Literal['ct-image-result-v1'] = 'ct-image-result-v1'
    renderer_version: str
    job_id: Identifier
    source_sha256: Sha256
    recipe_sha256: Sha256
    recipe: ImageRecipe
    output_sha256: Sha256
    output_ref: str
    report_ref: str
    source_format: str
    output_format: Literal['png', 'apng']
    display_width: Annotated[int, Field(ge=1)]
    display_height: Annotated[int, Field(ge=1)]
    source_exif_orientation: Annotated[int, Field(ge=1, le=8)] | None
    icc_profile_sha256: Sha256 | None
    frame_count: Annotated[int, Field(ge=1)]
    animation_frame_count: Annotated[int, Field(ge=0)]
    animation_plays: Annotated[int, Field(ge=0)] | None
    has_default_poster: bool
    frames: tuple[FrameResult, ...]
    privacy_status: Literal['pending'] = 'pending'
    quality_status: Literal['pending'] = 'pending'
    watermark_quality_status: Literal['pending', 'not_requested']
    face_detection_performed: Literal[False] = False
    privacy_confirmation: Literal[False] = False
    quality_confirmation: Literal[False] = False
    license_confirmation: Literal[False] = False
    watermark_restoration_performed: Literal[False] = False
    effective_values_written: Literal[False] = False

    @model_validator(mode='after')
    def check(self):
        if self.recipe_sha256 != recipe_fingerprint(self.renderer_version, self.recipe):
            raise ValueError('processing recipe fingerprint mismatch')
        if len(self.frames) != self.frame_count or tuple(f.frame_index for f in self.frames) != tuple(range(self.frame_count)):
            raise ValueError('all ordered output frames required')
        animated = sum(f.role == 'animation' for f in self.frames)
        posters = [f.frame_index for f in self.frames if f.role == 'poster']
        if animated != self.animation_frame_count or bool(posters) != self.has_default_poster or posters not in ([], [0]):
            raise ValueError('animation/poster structure mismatch')
        if (animated > 0) != (self.animation_plays is not None):
            raise ValueError('animation playback count required')
        if not animated and (self.frame_count != 1 or self.frames[0].role != 'still'):
            raise ValueError('a still result contains exactly one still frame')
        if self.output_format != self.recipe.output_format or (self.output_format == 'apng') != (animated > 0):
            raise ValueError('output format must match the explicit recipe and animation structure')
        expected = f'jobs/{self.job_id}/image-processing/image-{self.source_sha256[:16]}-{self.recipe_sha256[:16]}'
        if self.output_ref != expected+'.png' or self.report_ref != expected+'.json':
            raise ValueError('output/report must belong to this source, recipe and job')
        validate_relative_path(self.output_ref)
        validate_relative_path(self.report_ref)
        for frame in self.frames:
            declared = next((item for item in self.recipe.unmasked_frame_declarations if item.frame_index == frame.frame_index), None)
            if frame.unmasked_frame_declaration != declared:
                raise ValueError('result must retain the exact recipe declaration for each frame')
            for region in frame.regions:
                if region.box_xyxy[2] > self.display_width or region.box_xyxy[3] > self.display_height:
                    raise ValueError('applied region outside output canvas')
        return self
