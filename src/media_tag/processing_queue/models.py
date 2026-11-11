from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.cleaning.models import CleaningLimits, CleaningRecipe
from media_tag.contracts.models import ArtifactRef, ContractModel, Identifier, PositiveInt, Sha256
from media_tag.image_processing.models import ImageRecipe, ProcessingLimits
from .derived_contracts import Reviewers

VERSION = 'ct-processing-queue-v1'
STAGES = ('cleaning_output', 'image_mask_output')


class ProcessingDeployment(ContractModel):
    enabled: bool = False
    timeout_seconds: Annotated[int, Field(ge=3, le=600)] = 300
    cleaning: CleaningLimits = CleaningLimits()
    image: ProcessingLimits = ProcessingLimits()
    auto_adopt: bool = False
    reviewers: Reviewers = Reviewers()


class SourceSubmission(ContractModel):
    request_id: Identifier
    asset_id: Identifier
    expected_revision: PositiveInt
    source_sha256: Sha256


class CleaningSubmission(SourceSubmission):
    recipe: CleaningRecipe

    @model_validator(mode='after')
    def matching_source(self):
        if self.recipe.source_sha256 != self.source_sha256:
            raise ValueError('cleaning recipe must bind the requested catalogue source hash')
        return self


class ImageSubmission(SourceSubmission):
    recipe: ImageRecipe


class SourceTask(ContractModel):
    expected_revision: PositiveInt
    source_sha256: Sha256
    requester_id: Identifier
    source_owner_id: Identifier


class CleaningTask(SourceTask):
    operation: Literal['cleaning'] = 'cleaning'
    recipe: CleaningRecipe
    limits: CleaningLimits


class ImageTask(SourceTask):
    operation: Literal['image_mask'] = 'image_mask'
    recipe: ImageRecipe
    limits: ProcessingLimits


class ProcessingReport(ContractModel):
    schema_version: Literal['ct-processing-report-v1'] = 'ct-processing-report-v1'
    task_id: Identifier
    job_id: Identifier
    lease_epoch: PositiveInt
    asset_id: Identifier
    asset_revision: PositiveInt
    source_sha256: Sha256
    input_fingerprint: Sha256
    recipe_sha256: Sha256
    renderer_version: str
    operation: Literal['cleaning', 'image_mask']
    state: Literal['pending_registration_review', 'waiting_policy', 'waiting_evidence', 'failed']
    source_bytes_verified: bool
    output: ArtifactRef | None = None
    output_bytes: PositiveInt | None = None
    core_report: ArtifactRef | None = None
    summary: dict = Field(default_factory=dict)
    issue_code: str | None = None
    asset_registered: Literal[False] = False
    registration_status: Literal['pending_registration', 'not_produced']
    review_status: Literal['pending', 'not_produced']
    output_download_available: Literal[False] = False
    original_preserved: Literal[True] = True
    privacy_confirmation: Literal[False] = False
    quality_confirmation: Literal[False] = False
    medical_confirmation: Literal[False] = False
    license_status: Literal['unverified'] = 'unverified'
    effective_values_written: Literal[False] = False

    @model_validator(mode='after')
    def consistent_output(self):
        ready = self.state == 'pending_registration_review'
        if ready != (self.output is not None and self.output_bytes is not None and self.core_report is not None):
            raise ValueError('only actual produced output may wait for registration/review')
        if ready and (not self.source_bytes_verified or self.registration_status != 'pending_registration' or self.review_status != 'pending' or self.issue_code is not None):
            raise ValueError('produced artifact requires verified source and pending registration/review')
        if not ready and (self.output is not None or self.output_bytes is not None or self.core_report is not None
                or self.registration_status != 'not_produced' or self.review_status != 'not_produced' or not self.issue_code):
            raise ValueError('unresolved processing cannot supply a completed artifact')
        return self
