from typing import Annotated, Literal
from pydantic import Field
from media_tag.contracts.models import ContractModel, FieldValue, Identifier, Sha256

Note = Annotated[str, Field(min_length=1, max_length=2000, pattern=r'\S')]


class ReviewDeployment(ContractModel):
    enabled: bool = False
    reviewers: tuple[Identifier, ...] = ()
    people: dict[Identifier, Identifier] = Field(default_factory=dict)
    taxonomy_manifest: str | None = None
    taxonomy_version: str | None = None
    taxonomy_manifest_sha256: Sha256 | None = None
    trusted_publishers: tuple[Identifier, ...] = ()


class Observation(ContractModel):
    request_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    source_sha256: Sha256
    kind: Literal['image', 'video_segment', 'audio_segment', 'human_record']
    note: Note

    viewed: bool = False
    start_seconds: Annotated[float, Field(ge=0)] | None = None
    end_seconds: Annotated[float, Field(gt=0)] | None = None
    reference: str | None = None


class FieldDecision(ContractModel):
    request_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    source_sha256: Sha256
    field: Annotated[str, Field(min_length=1, max_length=100)]
    decision: Literal['manual', 'accept', 'reject'] = 'manual'
    value: FieldValue | None = None
    observation_ids: Annotated[tuple[Identifier, ...], Field(max_length=20)] = ()
    run_id: Identifier | None = None
    suggestion_id: Identifier | None = None
    note: Note
    dynamic: bool = False


class Confirmation(ContractModel):
    request_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    source_sha256: Sha256
    decision: Literal['confirm', 'revoke']
    full_source_viewed: bool = False
    note: Note


class GapSubmission(ContractModel):
    request_id: Identifier
    values: dict[str, FieldValue]
    note: Note


class GapRevision(GapSubmission):
    expected_revision: Annotated[int, Field(ge=1)]
