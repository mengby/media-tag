from typing import Annotated,Literal
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel,Identifier,Sha256,PositiveInt
from media_tag.terminology.contracts import Context,Note,RelationKind
from media_tag.terminology.resolve.models import ResolveInput


class SearchOptions(ContractModel):
    enabled: bool=False
    max_assets: Annotated[int,Field(strict=True,ge=1,le=50000)]=5000


class AssetFilters(ContractModel):
    media_kinds: Annotated[tuple[Literal['image','gif','animation','video','audio'],...],Field(max_length=5)]=()
    format_names: Annotated[tuple[Annotated[str,Field(pattern=r'^[a-z0-9_]{1,64}$')],...],Field(max_length=20)]=()
    min_file_duration_seconds: Annotated[float,Field(ge=0,le=864000)] | None=None
    max_file_duration_seconds: Annotated[float,Field(ge=0,le=864000)] | None=None
    min_display_width: Annotated[int,Field(strict=True,ge=1,le=65535)] | None=None
    min_display_height: Annotated[int,Field(strict=True,ge=1,le=65535)] | None=None
    has_audio: bool | None=None
    has_transparent_pixels: bool | None=None
    usages: Annotated[tuple[Annotated[str,Field(min_length=1,max_length=200)],...],Field(max_length=20)]=()
    exclude_asset_ids: Annotated[tuple[Identifier,...],Field(max_length=100)]=()
    require_redacted: bool=False

    @model_validator(mode='after')
    def duration_range(self):
        if self.min_file_duration_seconds is not None and self.max_file_duration_seconds is not None and self.min_file_duration_seconds>self.max_file_duration_seconds:
            raise ValueError('file duration range inverted')
        return self


class SearchRequest(ContractModel):
    request: ResolveInput
    selected_concept_ids: Annotated[tuple[Identifier,...],Field(max_length=20)]=()
    selection_lexicon_release_id: Identifier | None=None
    literal_only: bool=False
    include_drafts: bool=False
    include_visual_drafts: bool=False
    mine: bool=False
    workspace: bool=False
    limit: Annotated[int,Field(strict=True,ge=1,le=50)]=30
    cursor: Annotated[str,Field(max_length=2048)]=''
    filters: AssetFilters=AssetFilters()
    prefer_confirmed: bool=False
    request_context_sha256: Sha256 | None=None


class ConceptLink(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    source_sha256: Sha256
    lexicon_release_id: Identifier
    concept_id: Identifier
    polarity: Literal['affirmed','negated']='affirmed'
    direction: Literal['unspecified','static','increasing','decreasing','transition']='unspecified'
    observation_ids: Annotated[tuple[Identifier,...],Field(min_length=1,max_length=20)]
    start_seconds: Annotated[float,Field(ge=0)] | None=None
    end_seconds: Annotated[float,Field(gt=0)] | None=None
    note: Note

    @model_validator(mode='after')
    def interval(self):
        if (self.start_seconds is None)!=(self.end_seconds is None) or self.start_seconds is not None and self.end_seconds<=self.start_seconds:
            raise ValueError('positive interval required')
        return self


class RevokeLink(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    source_sha256: Sha256
    note: Note


class Feedback(ContractModel):
    request_id: Identifier
    expression: Annotated[str,Field(min_length=1,max_length=2000,pattern=r'\S')]
    quote: Annotated[str,Field(max_length=2000)]=''
    target_id: Identifier | None=None
    reported_target_id: Identifier | None=None
    reported_entry_id: Identifier | None=None
    reported_lexicon_release_id: Identifier | None=None
    lexicon_release_id: Identifier | None=None
    relation: RelationKind='equivalent'
    context: Context=Context()
    kind: Literal['add_expression','incorrect_mapping']='add_expression'


class FeedbackRevision(Feedback):
    expected_revision: PositiveInt
    withdraw: bool=False


class FeedbackDecision(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    action: Literal['needs_info','reject','promote']
    note: Note


class ImportMapping(ContractModel):
    request_id: Identifier
    candidate_id: Identifier
    expected_revision: PositiveInt
    sheet: str | None=None
    columns: dict[str,str]
    defaults: dict[str,str]=Field(default_factory=dict)
    selected_rows: Annotated[tuple[PositiveInt,...],Field(max_length=1000)]=()
    apply: bool=False
    note: Note


class DraftTrial(ContractModel):
    expected_revision: PositiveInt
    request: SearchRequest
