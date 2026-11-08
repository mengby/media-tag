"""Strict, bounded D13 contracts; taxonomy and lexicon have separate versions."""
from typing import Annotated, Literal, Self
import unicodedata

from pydantic import Field, model_validator
from media_tag.contracts.models import ContractModel, Identifier, Sha256, PositiveInt
from media_tag.catalog.models import SourceInput

Label = Annotated[str, Field(min_length=1, max_length=200, pattern=r'\S')]
Note = Annotated[str, Field(min_length=1, max_length=2000, pattern=r'\S')]
Kind = Literal['taxonomy', 'lexicon']
RelationKind = Literal['equivalent', 'contextual', 'broader', 'narrower', 'related', 'metaphor']


def normalize(text: str) -> str:
    # Do not remove negation, direction, punctuation, or anatomical distinctions.
    return ' '.join(unicodedata.normalize('NFKC', text).split()).casefold()


class Source(ContractModel):
    kind: Literal['human', 'authorized_record', 'external_reference', 'synthetic']
    locator: Note
    quote: Note
    usage: Note
    reference: str | None = None

    @model_validator(mode='after')
    def valid_reference(self) -> Self:
        if self.reference: SourceInput(reference=self.reference)
        return self


class Entry(ContractModel):
    entry_id: Identifier
    sources: Annotated[tuple[Source, ...], Field(max_length=20)] = ()
    rationale: Note


class TaxonomyOption(Entry):
    field: Label
    value: Label
    status: Literal['active', 'retired'] = 'active'
    replacements: Annotated[tuple[Identifier, ...], Field(max_length=20)] = ()


class Taxonomy(ContractModel):
    schema_version: Literal['d13-taxonomy-v1'] = 'd13-taxonomy-v1'
    template_fingerprint: Sha256
    options: Annotated[tuple[TaxonomyOption, ...], Field(max_length=5000)] = ()


class Context(ContractModel):
    departments: tuple[Identifier, ...] = ()
    organs: tuple[Identifier, ...] = ()
    polarity: Literal['unspecified', 'affirmed', 'negated'] = 'unspecified'
    direction: Literal['unspecified', 'static', 'increasing', 'decreasing', 'transition'] = 'unspecified'
    required_terms: Annotated[tuple[Label, ...], Field(max_length=20)] = ()
    excluded_terms: Annotated[tuple[Label, ...], Field(max_length=20)] = ()


class Concept(Entry):
    name: Label
    concept_type: Literal['department', 'organ', 'symptom', 'disease', 'process', 'state', 'other']
    definition: Note
    scope: Note
    option_ids: Annotated[tuple[Identifier, ...], Field(max_length=30)] = ()
    old_names: Annotated[tuple[Label, ...], Field(max_length=30)] = ()
    status: Literal['active', 'retired'] = 'active'
    replacements: Annotated[tuple[Identifier, ...], Field(max_length=20)] = ()


class Alias(Entry):
    expression: Label
    target_id: Identifier | None = None  # A submitter may not know the professional name.
    relation: RelationKind
    context: Context = Context()
    default_expand: bool = False
    ambiguity_group: Identifier | None = None


class Relation(Entry):
    source_id: Identifier
    target_id: Identifier
    relation: RelationKind
    default_expand: bool = False


class Lexicon(ContractModel):
    schema_version: Literal['d13-lexicon-v1'] = 'd13-lexicon-v1'
    template_fingerprint: Sha256
    taxonomy_release_id: Identifier
    concepts: Annotated[tuple[Concept, ...], Field(max_length=5000)] = ()
    aliases: Annotated[tuple[Alias, ...], Field(max_length=10000)] = ()
    relations: Annotated[tuple[Relation, ...], Field(max_length=10000)] = ()


class Deployment(ContractModel):
    enabled: bool = False
    managers: tuple[Identifier, ...] = ()
    publishers: tuple[Identifier, ...] = ()


class CandidateSubmission(ContractModel):
    request_id: Identifier
    kind: Kind
    base_release_id: Identifier | None = None
    payload: Taxonomy | Lexicon
    note: Note

    @model_validator(mode='after')
    def matching_kind(self) -> Self:
        if (self.kind == 'taxonomy') != isinstance(self.payload, Taxonomy):
            raise ValueError('candidate kind does not match payload')
        return self


class CandidateRevision(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    payload: Taxonomy | Lexicon
    note: Note


class Review(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    action: Literal['submit', 'approve', 'reject']
    entry_ids: Annotated[tuple[Identifier, ...], Field(max_length=25000)] = ()
    note: Note


class Publish(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    expected_generation: Annotated[int, Field(strict=True, ge=0)]
    version: Identifier
    note: Note


class Rollback(ContractModel):
    request_id: Identifier
    expected_generation: Annotated[int, Field(strict=True, ge=0)]
    taxonomy_release_id: Identifier
    lexicon_release_id: Identifier | None = None
    note: Note


class ImportRows(ContractModel):
    request_id: Identifier
    expected_revision: PositiveInt
    entity: Literal['options', 'concepts', 'aliases', 'relations']
    rows: Annotated[tuple[dict, ...], Field(min_length=1, max_length=1000)]
    apply: bool = False
    note: Note
