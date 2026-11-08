"""Bounded D14 query, index activation, deployment and task configuration."""
from typing import Annotated, Literal
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel,Identifier,ArtifactRef
from media_tag.terminology.contracts import Context

PREPROCESS_VERSION='d14-cls-l2-v1'
MATCH_VERSION='d14-conservative-v1'


class QueryInputError(ValueError):
    """A bounded caller query error, distinct from corrupt server metadata."""


class ResolveInput(ContractModel):
    query: Annotated[str,Field(min_length=1,max_length=2000,pattern=r'\S')]
    purpose: Literal['search','tag']='search'
    evidence_origin: Literal['human_input','ocr','visual_description','old_filename','prior_quote']='human_input'
    evidence_ref: ArtifactRef | None=None
    context: Context=Context()
    concept_types: tuple[Literal['department','organ','symptom','disease','process','state','other'],...]=()
    top_k: Annotated[int,Field(strict=True,ge=1,le=20)]=10
    use_model: bool=True


class ResolveOptions(ContractModel):
    enabled: bool=False
    model_enabled: bool=False
    model_root: str='cache/models/d14'
    query_timeout_seconds: Annotated[float,Field(gt=0,le=30)]=8.0
    build_timeout_seconds: Annotated[float,Field(gt=0,le=300)]=120.0
    min_cosine: Annotated[float,Field(ge=-1,le=1)]=0.75
    min_margin: Annotated[float,Field(ge=0,le=2)]=0.05
    cache_entries: Annotated[int,Field(strict=True,ge=0,le=512)]=128


class IndexActivation(ContractModel):
    request_id: Identifier
    expected_generation: Annotated[int,Field(strict=True,ge=0)]
    expected_serving_epoch: Annotated[int,Field(strict=True,ge=0)]
    taxonomy_release_id: Identifier
    lexicon_release_id: Identifier
    note: Annotated[str,Field(min_length=1,max_length=2000,pattern=r'\S')]


class ResolveTask(ContractModel):
    taxonomy_release_id: Identifier
    lexicon_release_id: Identifier
    embedding_index_id: Identifier | None=None
    request: ResolveInput

    @model_validator(mode='after')
    def tag_only(self):
        if self.request.purpose!='tag':raise ValueError('asset task must have tag purpose')
        return self
