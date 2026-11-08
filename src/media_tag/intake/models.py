from datetime import datetime
from typing import Annotated,Literal
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel,Sha256,Identifier,validate_relative_path


class IntakeError(Exception):
    def __init__(self,code,*,retryable=False,status=None):
        self.code,self.retryable,self.status=code,retryable,status
        super().__init__(code)


class InventoryEntry(ContractModel):
    key: str
    state: Literal['ready','unsupported','excluded','failed']
    size_bytes: Annotated[int,Field(ge=0)]=0
    sha256: Sha256 | None=None
    mtime_ns: int | None=None
    device: int | None=None
    inode: int | None=None
    signature_hint: str | None=None
    duplicate_of: str | None=None
    error_code: str | None=None

    @model_validator(mode='after')
    def ready(self):
        if self.state=='ready':
            validate_relative_path(self.key)
            if self.sha256 is None or self.size_bytes<1 or any(v is None for v in (self.mtime_ns,self.device,self.inode)):
                raise ValueError('ready inventory entries require immutable source identity')
        return self


class Inventory(ContractModel):
    schema_version: Literal['local-inventory-v1']='local-inventory-v1'
    batch_id: Identifier
    source_root: str
    created_at: datetime
    entries: Annotated[tuple[InventoryEntry,...],Field(max_length=100000)]

    @model_validator(mode='after')
    def unique(self):
        if self.created_at.tzinfo is None or len({r.key for r in self.entries})!=len(self.entries):
            raise ValueError('timezone and unique inventory paths required')
        by_key={r.key:r for r in self.entries}
        for row in self.entries:
            if row.duplicate_of and (row.duplicate_of not in by_key or row.duplicate_of==row.key or by_key[row.duplicate_of].sha256!=row.sha256):
                raise ValueError('invalid duplicate binding')
        return self
