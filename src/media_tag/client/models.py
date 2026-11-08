"""Task-local acquisition and current-use handoff. No host approval is inferred."""
from datetime import datetime,timezone
from typing import Annotated,Literal
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel,Identifier,Sha256 as SHA256
from media_tag.files.models import PlaybackInterval


class UseContext(ContractModel):
    unit_id: Identifier
    transcript_sha256: SHA256
    candidate_sha256: SHA256
    transcript_path: Annotated[str,Field(min_length=1,max_length=1024)]='edit/cut.transcript.json'
    candidate_path: Annotated[str,Field(min_length=1,max_length=1024)]='edit/candidate.json'
    quote: Annotated[str,Field(min_length=1,max_length=8000)]
    purpose: Annotated[str,Field(min_length=1,max_length=2000)]
    timeline: PlaybackInterval
    source_interval: PlaybackInterval | None=None


class Acquisition(ContractModel):
    asset_id: Identifier
    sha256: SHA256
    context: UseContext
    search_versions: dict[Annotated[str,Field(min_length=1,max_length=100)],Annotated[str,Field(max_length=256)]|int|None]=Field(default_factory=dict,max_length=24)


class CheckItem(ContractModel):
    status: Literal['pass','fail','not_applicable','skipped']
    observation: Annotated[str,Field(min_length=1,max_length=4000)]


class CheckEvidence(ContractModel):
    path: Annotated[str,Field(min_length=1,max_length=1024)]
    sha256: SHA256


class CurrentUseCheck(ContractModel):
    receipt_sha256: SHA256
    context: UseContext
    source_sha256: SHA256
    decision: Literal['pass','reject']
    reviewer_type: Literal['model','human']
    reviewed_by: Identifier
    reviewed_at: datetime
    actual_media_observed: bool
    evidence: Annotated[tuple[CheckEvidence,...],Field(min_length=1,max_length=64)]
    checks: dict[str,CheckItem]=Field(min_length=10,max_length=10)

    @model_validator(mode='after')
    def complete_checks(self):
        if self.reviewed_at.tzinfo is None or self.reviewed_at>datetime.now(timezone.utc):raise ValueError('actual nonfuture timezone timestamp required')
        if set(self.checks)!={'semantic','action','timing','composition','packaging','technical','style','source','license','eye_mosaic'}:
            raise ValueError('all ten current-use checks required')
        if any(k!='license' and v.status=='skipped' for k,v in self.checks.items()):raise ValueError('only license can be skipped by explicit policy')
        if self.decision=='pass' and (any(self.checks[k].status!='pass' for k in ('semantic','source','technical')) or any(v.status=='fail' for v in self.checks.values())):
            raise ValueError('mandatory checks cannot be waived or failed')
        return self
