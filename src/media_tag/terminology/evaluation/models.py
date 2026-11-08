from datetime import datetime
from typing import Annotated,Literal
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel,Identifier,Sha256
from media_tag.terminology.contracts import Context,Note,normalize


class Pin(ContractModel):
    taxonomy_release_id: Identifier
    lexicon_release_id: Identifier
    embedding_index_id: Identifier
    model_revision: Sha256
    preprocess_version: Identifier
    match_version: Identifier
    resolution_policy_sha256: Sha256


class Case(ContractModel):
    case_id: Identifier
    source_group: Identifier
    category: Literal['common','ambiguous','challenge','out_of_scope']
    query: Annotated[str,Field(min_length=1,max_length=2000,pattern=r'\S')]
    context: Context=Context()
    expectation: Literal['clear','clarify','reject']
    acceptable_concepts: tuple[Identifier,...]
    allowed_automatic_concepts: tuple[Identifier,...]
    forbidden_concepts: tuple[Identifier,...]=()
    relevant_asset_ids: tuple[Identifier,...]=()
    forbidden_asset_ids: tuple[Identifier,...]=()
    complete_relevant_set: bool=False
    expected_polarity: Literal['affirmed','negated'] | None=None
    expected_direction: Literal['unspecified','static','increasing','decreasing','transition'] | None=None
    annotation_note: Note

    @model_validator(mode='after')
    def meaning(self):
        for values in (self.acceptable_concepts,self.allowed_automatic_concepts,self.forbidden_concepts,self.relevant_asset_ids,self.forbidden_asset_ids):
            if len(values)!=len(set(values)):raise ValueError('annotation sets must be unique')
        if not set(self.allowed_automatic_concepts)<=set(self.acceptable_concepts):raise ValueError('automatic targets must be acceptable')
        if set(self.acceptable_concepts)&set(self.forbidden_concepts) or set(self.relevant_asset_ids)&set(self.forbidden_asset_ids):raise ValueError('contradictory annotation')
        if self.expectation=='clear' and (not self.acceptable_concepts or not self.allowed_automatic_concepts):raise ValueError('clear cases need explicit acceptable automatic targets')
        if self.expectation!='clear' and self.allowed_automatic_concepts:raise ValueError('clarify/reject cannot allow automatic expansion')
        if self.category=='common' and self.expectation!='clear' or self.category=='ambiguous' and self.expectation!='clarify' or self.category=='out_of_scope' and self.expectation!='reject':
            raise ValueError('category and expectation disagree')
        return self


class Criteria(ContractModel):
    # Defaults are the published proposed baseline, never evidence of user approval.
    approved_by: Identifier | None=None
    approval_reference: Note | None=None
    precision: Annotated[float,Field(ge=0,le=1)]=.95
    common_coverage: Annotated[float,Field(ge=0,le=1)]=.8
    concept_recall5: Annotated[float,Field(ge=0,le=1)]=.9
    asset_hit10: Annotated[float,Field(ge=0,le=1)]=.9
    counts: dict[Literal['common','ambiguous','challenge','out_of_scope'],Annotated[int,Field(strict=True,ge=1)]]=Field(
        default_factory=lambda:dict(common=60,ambiguous=20,challenge=20,out_of_scope=20))

    @model_validator(mode='after')
    def approval(self):
        if bool(self.approved_by)!=bool(self.approval_reference):raise ValueError('approval identity and reference required together')
        if set(self.counts)!={'common','ambiguous','challenge','out_of_scope'}:raise ValueError('all four category counts must be frozen')
        return self


class Dataset(ContractModel):
    schema_version: Literal['d16-evaluation-v1']='d16-evaluation-v1'
    dataset_id: Identifier
    split: Literal['calibration','evaluation']
    purpose: Literal['synthetic_engineering','business']
    frozen_at: datetime
    annotators: Annotated[tuple[Identifier,...],Field(min_length=1)]
    adjudicator: Identifier
    annotation_reference: Note
    independently_human_annotated: bool
    pin: Pin
    candidate_pool_sha256: Sha256
    calibration_sha256: Sha256 | None=None
    criteria: Criteria=Criteria()
    cases: Annotated[tuple[Case,...],Field(min_length=1,max_length=10000)]

    @model_validator(mode='after')
    def frozen(self):
        if self.frozen_at.tzinfo is None:raise ValueError('timezone required for freeze time')
        if len({c.case_id for c in self.cases})!=len(self.cases):raise ValueError('unique case ids required')
        if len({normalize(c.query) for c in self.cases})!=len(self.cases):raise ValueError('duplicate normalized queries')
        if self.purpose=='business' and not self.independently_human_annotated:raise ValueError('business answers must be independently human annotated')
        if self.split=='evaluation' and not self.calibration_sha256:raise ValueError('evaluation must bind a separate calibration file hash')
        return self
