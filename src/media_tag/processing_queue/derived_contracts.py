from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.catalog.service import encode
from media_tag.cleaning.models import CleaningRecipe
from media_tag.contracts.models import ContractModel, Identifier, PositiveInt, Sha256
from media_tag.image_processing.models import ImageRecipe

CONTRACT_VERSION = 'ct-processing-variant-v1'


class Reviewers(ContractModel):
    quality: tuple[Identifier,...] = ()
    watermark: tuple[Identifier,...] = ()
    privacy: tuple[Identifier,...] = ()


class ProcessingTimeMap(ContractModel):
    state: Literal['available','not_applicable']
    mapping: dict | None = None
    reason: Literal['image_has_no_video_clock','animation_timing_preserved_without_video_retime','audio_not_retimed'] | None = None

    @model_validator(mode='after')
    def consistent(self):
        if (self.state=='available') != (self.mapping is not None) or (self.state=='not_applicable') != (self.reason is not None):
            raise ValueError('explicit applicable video map or nonvideo reason required')
        return self


class ReviewRequirements(ContractModel):
    quality: Literal[True] = True
    watermark: bool
    privacy: bool
    audio_voice_privacy_reviewed: Literal[False] = False
    license_confirmation: Literal[False] = False


class FaceProcessingProof(ContractModel):
    core_report_sha256: Sha256
    applied: bool
    face_region_count: Annotated[int,Field(ge=0)]
    face_frame_indices: tuple[Annotated[int,Field(ge=0)],...]
    frame_count: Annotated[int,Field(ge=0)]
    face_detection_performed: Literal[False] = False

    @model_validator(mode='after')
    def consistent(self):
        if self.applied!=(self.face_region_count>0) or self.applied!=bool(self.face_frame_indices):
            raise ValueError('actual applied face regions required')
        if tuple(sorted(set(self.face_frame_indices)))!=self.face_frame_indices or any(index>=self.frame_count for index in self.face_frame_indices):
            raise ValueError('actual ordered face frame coverage required')
        return self


class ProcessingVariantContract(ContractModel):
    schema_version: Literal['ct-processing-variant-v1'] = CONTRACT_VERSION
    operation: Literal['cleaning','image_mask']
    parent_asset_id: Identifier
    source_sha256: Sha256
    source_revision: PositiveInt
    source_owner_id: Identifier
    requester_id: Identifier
    recipe: dict
    recipe_sha256: Sha256
    renderer_version: str
    implementation_version: str
    producer_task_id: Identifier
    producer_lease_epoch: PositiveInt
    producer_input_fingerprint: Sha256
    producer_result_sha256: Sha256
    output_sha256: Sha256
    output_bytes: PositiveInt
    content_kind: Literal['image','animation','video','audio']
    time_map: ProcessingTimeMap
    cumulative_speed: Annotated[float,Field(gt=0,allow_inf_nan=False)] | None
    review_requirements: ReviewRequirements
    face_processing: FaceProcessingProof
    original_preserved: Literal[True] = True
    human_values_inherited: Literal[False] = False
    license_confirmation: Literal[False] = False

    @model_validator(mode='after')
    def consistent(self):
        from hashlib import sha256
        recipe_type = CleaningRecipe if self.operation=='cleaning' else ImageRecipe
        recipe = recipe_type.model_validate_json(encode(self.recipe))
        if sha256(encode(recipe.model_dump(mode='json')).encode()).hexdigest()!=self.recipe_sha256:
            raise ValueError('derived recipe hash changed')
        if self.operation=='cleaning' and recipe.source_sha256!=self.source_sha256:
            raise ValueError('cleaning source binding changed')
        if (self.content_kind=='video') != (self.time_map.state=='available') or (self.content_kind=='video') != (self.cumulative_speed is not None):
            raise ValueError('video requires actual mapping; nonvideo cannot masquerade as retimed video')
        if self.review_requirements.privacy != (self.content_kind!='audio'):
            raise ValueError('visual privacy pending; pure audio face review not applicable')
        if self.face_processing.applied and (self.operation!='image_mask' or not any(region.purpose=='face' for region in recipe.regions)):
            raise ValueError('privacy approval or cleaning alone cannot claim face masking')
        return self


class OutputReview(ContractModel):
    expected_revision: PositiveInt
    output_sha256: Sha256
    recipe_hash: Sha256
    contract_version: Literal['ct-processing-variant-v1'] = CONTRACT_VERSION
    dimension: Literal['quality','watermark','privacy']
    decision: Literal['approved','revoked']
    full_output_viewed: bool = False
    source_compared: bool = False
    no_unintended_changes: bool = False
    watermark_regions_checked: bool = False
    face_coverage_checked: bool = False
    note: Annotated[str,Field(max_length=2000)] = ''
