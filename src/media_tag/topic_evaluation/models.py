from datetime import datetime
from collections.abc import Mapping
from typing import Annotated,Literal
from pydantic import Field,model_validator
from media_tag.analysis.contracts import TOPIC_FIELDS,TopicField,ContentKind
from media_tag.contracts.models import ContractModel,Identifier,Sha256,Versions,ArtifactRef

Text=Annotated[str,Field(min_length=1,max_length=1000,pattern=r'\S')]
Label=Annotated[str,Field(min_length=1,max_length=200,pattern=r'\S')]
Split=Literal['training','calibration','acceptance']


class Gold(ContractModel):
    field: TopicField
    state: Literal['labelled','unassessable','not_applicable']
    complete_value_set: bool
    values: Annotated[tuple[Label,...],Field(max_length=16)] = ()
    forbidden_values: Annotated[tuple[Label,...],Field(max_length=16)] = ()
    opposite_direction_values: Annotated[tuple[Label,...],Field(max_length=16)] = ()
    annotation_note: Text
    @model_validator(mode='after')
    def check(self):
        for values in (self.values,self.forbidden_values,self.opposite_direction_values):
            if len(set(values))!=len(values):raise ValueError('duplicate_gold_labels')
        if self.state!='labelled' and (self.values or self.forbidden_values or self.opposite_direction_values):
            raise ValueError('unassessable_gold_cannot_claim_labels')
        if self.complete_value_set!=(self.state=='labelled'):raise ValueError('labelled_gold_requires_complete_value_set')
        if set(self.values)&(set(self.forbidden_values)|set(self.opposite_direction_values)):
            raise ValueError('contradictory_gold_labels')
        return self


class Case(ContractModel):
    case_id: Identifier
    source_sha256: Sha256
    content_kind: ContentKind
    near_group: Identifier
    split: Split
    annotation_reference: Text
    gold: Annotated[tuple[Gold,...],Field(min_length=10,max_length=10)]
    @model_validator(mode='after')
    def check(self):
        if len({g.field for g in self.gold})!=10 or {g.field for g in self.gold}!=set(TOPIC_FIELDS):
            raise ValueError('exact_ten_gold_fields_required')
        for item in self.gold:
            na=self.content_kind=='audio' and item.field in ('画面主体','表现形式')
            if na and item.state!='not_applicable':
                raise ValueError('pure_audio_visual_fields_require_not_applicable')
        return self


class ProducerPin(ContractModel):
    versions: Versions
    prompt_version: Text
    @model_validator(mode='after')
    def check(self):
        if self.versions.model_revision is None:raise ValueError('actual_model_revision_required')
        return self


class Run(ContractModel):
    run_id: Identifier
    case_ids: Annotated[tuple[Identifier,...],Field(min_length=1,max_length=10000)] | None=None
    producer_pins: Annotated[tuple[ProducerPin,...],Field(min_length=1,max_length=32)]
    @model_validator(mode='after')
    def check(self):
        if len({pin.model_dump_json() for pin in self.producer_pins})!=len(self.producer_pins):
            raise ValueError('duplicate_run_pins')
        if self.case_ids is not None and len(set(self.case_ids))!=len(self.case_ids):raise ValueError('duplicate_run_case_scope')
        return self


class Dataset(ContractModel):
    schema_version: Literal['ct-topic-evaluation-dataset-v1']='ct-topic-evaluation-dataset-v1'
    dataset_id: Identifier
    purpose: Literal['synthetic_engineering','business']
    frozen_at: datetime
    annotators: Annotated[tuple[Identifier,...],Field(min_length=1,max_length=100)]
    adjudicator: Identifier
    independently_human_annotated: bool
    annotation_reference: Text
    training_membership_declared_complete: bool=False
    taxonomy_version: Text | None=None
    taxonomy_sha256: Sha256 | None=None
    runs: Annotated[tuple[Run,...],Field(min_length=1,max_length=16)]
    cases: Annotated[tuple[Case,...],Field(min_length=1,max_length=10000)]
    @model_validator(mode='after')
    def check(self):
        if self.frozen_at.tzinfo is None:raise ValueError('timezone_required')
        if (self.taxonomy_version is None)!=(self.taxonomy_sha256 is None):raise ValueError('taxonomy_version_hash_pair_required')
        if len(set(self.annotators))!=len(self.annotators):raise ValueError('duplicate_annotators')
        if len({r.run_id for r in self.runs})!=len(self.runs):raise ValueError('duplicate_runs')
        if len({c.case_id for c in self.cases})!=len(self.cases):raise ValueError('duplicate_cases')
        hashes={};groups={}
        for case in self.cases:
            if case.source_sha256 in hashes:
                if hashes[case.source_sha256]!=case.split:raise ValueError('exact_source_split_leakage')
                raise ValueError('duplicate_source_in_split')
            if case.near_group in groups and groups[case.near_group]!=case.split:raise ValueError('near_group_split_leakage')
            hashes[case.source_sha256]=case.split;groups[case.near_group]=case.split
        for run in self.runs:
            if run.case_ids is not None and not set(run.case_ids)<={c.case_id for c in self.cases}:
                raise ValueError('run_case_scope_outside_dataset')
            if any(pin.versions.taxonomy_version!=self.taxonomy_version for pin in run.producer_pins):
                raise ValueError('run_taxonomy_scope_mismatch')
        identities=(*self.annotators,self.adjudicator)
        if self.purpose=='synthetic_engineering':
            if any(not identity.casefold().startswith('synthetic-') for identity in identities):
                raise ValueError('synthetic_identities_must_be_explicit')
        elif not self.independently_human_annotated or any(identity.casefold().startswith('synthetic-') for identity in identities):
            raise ValueError('business_requires_declared_independent_actual_annotators')
        return self


class Prediction(ContractModel):
    case_id: Identifier
    source_sha256: Sha256
    content_kind: ContentKind
    document: ArtifactRef | None=None
    failure_code: Text | None=None
    elapsed_seconds: Annotated[float,Field(ge=0)] | None=None
    timing_scope: Literal['declared_pipeline_elapsed','sum_verified_producer_stage_seconds','not_measured']='declared_pipeline_elapsed'
    @model_validator(mode='before')
    @classmethod
    def default_unmeasured_timing(cls,data):
        if isinstance(data,Mapping) and 'timing_scope' not in data and data.get('elapsed_seconds') is None:
            return {**data,'timing_scope':'not_measured'}
        return data
    @model_validator(mode='after')
    def check(self):
        if (self.document is None)==(self.failure_code is None):raise ValueError('document_xor_explicit_failure_required')
        if self.document is not None and self.document.kind!='topic_analysis_document':raise ValueError('explicit_topic_document_required')
        if (self.elapsed_seconds is None)!=(self.timing_scope=='not_measured'):
            raise ValueError('explicit_unmeasured_timing_or_actual_elapsed_required')
        return self


class Export(ContractModel):
    schema_version: Literal['ct-topic-predictions-v1']='ct-topic-predictions-v1'
    purpose: Literal['synthetic_engineering','business']
    dataset_sha256: Sha256
    run_id: Identifier
    split_scope: Literal['calibration','acceptance'] | None=None
    taxonomy_version: Text | None=None
    taxonomy_sha256: Sha256 | None=None
    exported_at: datetime
    predictions: Annotated[tuple[Prediction,...],Field(max_length=10000)]
    @model_validator(mode='after')
    def check(self):
        if self.exported_at.tzinfo is None:raise ValueError('timezone_required')
        if (self.taxonomy_version is None)!=(self.taxonomy_sha256 is None):raise ValueError('taxonomy_version_hash_pair_required')
        if len({p.case_id for p in self.predictions})!=len(self.predictions):raise ValueError('duplicate_prediction_cases')
        return self
