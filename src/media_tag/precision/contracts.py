"""Provider-neutral local precision protocol; no provider may supply an approval."""
from hashlib import sha256
from pathlib import Path
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.analysis.contracts import AnalysisDocument, ContentKind, TopicField
from media_tag.contracts.models import ContractModel, Evidence, FieldValue, Identifier, Sha256, Text

VERSION = 'ct-precision-v1'
PROMPT = 'ct-precision-prompt-v1'


class PrecisionProvider(ContractModel):
    provider_id: Identifier
    adapter_path: str
    adapter_sha256: Sha256
    model_id: Text
    model_revision: Text
    inputs: Annotated[tuple[ContentKind, ...], Field(min_length=1, max_length=4)]
    device: Literal['cpu', 'cuda'] = 'cpu'
    execution: Literal['local'] = 'local'

    @model_validator(mode='after')
    def check(self):
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError('unique provider inputs required')
        path = Path(self.adapter_path)
        if not path.is_absolute() or path.suffix.lower() != '.py':
            raise ValueError('absolute local Python bridge required')
        return self

    @property
    def revision(self):
        value = self.model_dump(mode='json', exclude={'adapter_path'})
        value['inputs'] = sorted(value['inputs'])
        return sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


class PrecisionDeployment(ContractModel):
    enabled: bool = False
    auto_dispatch: bool = False
    dispatch_limit: Annotated[int, Field(ge=1, le=256)] = 32
    providers: Annotated[tuple[PrecisionProvider, ...], Field(max_length=8)] = ()
    visual_provider: Identifier | None = None
    audio_provider: Identifier | None = None
    timeout_seconds: Annotated[int, Field(ge=1, le=600)] = 120
    max_input_bytes: Annotated[int, Field(ge=1, le=512*1024**2)] = 128*1024**2
    max_request_bytes: Annotated[int, Field(ge=1024, le=8*1024**2)] = 1024**2
    max_duration_seconds: Annotated[float, Field(gt=0, le=600)] = 30.0

    @model_validator(mode='after')
    def check(self):
        ids = {p.provider_id for p in self.providers}
        if len(ids) != len(self.providers) or any(p is not None and p not in ids for p in (self.visual_provider, self.audio_provider)):
            raise ValueError('unique explicitly configured providers required')
        return self


class PrecisionOptions(ContractModel):
    provider_id: Identifier
    provider_revision: Sha256
    policy_digest: Sha256
    content_kind: ContentKind


class PrecisionField(ContractModel):
    field: TopicField
    value: FieldValue
    candidates: Annotated[tuple[Annotated[str, Field(min_length=1, max_length=200)], ...], Field(max_length=16)] = ()
    dynamic: bool = False

    @model_validator(mode='after')
    def check(self):
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError('unique precision candidates required')
        if self.candidates and self.value.state not in ('unknown', 'no_match'):
            raise ValueError('unmapped candidates require an unresolved field')
        return self


class PrecisionReply(ContractModel):
    schema_version: Literal['ct-precision-reply-v1'] = 'ct-precision-reply-v1'
    request_id: Identifier
    source_sha256: Sha256
    provider_revision: Sha256
    fields: Annotated[tuple[PrecisionField, ...], Field(min_length=1, max_length=10)]
    usage: 'ProviderUsage | None' = None

    @model_validator(mode='after')
    def check(self):
        if len({f.field for f in self.fields}) != len(self.fields):
            raise ValueError('unique precision fields required')
        return self


class ProviderUsage(ContractModel):
    """Unverified provider accounting; absence never means free or zero tokens."""

    input_tokens: Annotated[int, Field(ge=0)] | None = None
    output_tokens: Annotated[int, Field(ge=0)] | None = None
    cost_micros: Annotated[int, Field(ge=0)] | None = None
    currency: Annotated[str, Field(pattern=r'^[A-Z]{3}$')] | None = None
    verified: Literal[False] = False

    @model_validator(mode='after')
    def check(self):
        if (self.cost_micros is None) != (self.currency is None):
            raise ValueError('reported cost requires currency and amount together')
        return self


PrecisionReply.model_rebuild()


class PrecisionReport(ContractModel):
    schema_version: Literal['ct-precision-report-v1'] = 'ct-precision-report-v1'
    analysis_layer: Literal['precision', 'local_audio'] = 'precision'
    request_id: Identifier
    provider: PrecisionProvider
    policy_digest: Sha256
    prompt_version: Literal['ct-precision-prompt-v1'] = PROMPT
    document: AnalysisDocument
    reply: PrecisionReply | None = None
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0.0
    usage: ProviderUsage | None = None
