from typing import Annotated, Literal

from pydantic import Field, model_validator

from media_tag.catalog.models import SourceInput
from media_tag.contracts.models import ContractModel, Identifier, Sha256, Text, validate_relative_path


class UploadSpec(ContractModel):
    request_id: Identifier
    filename: Annotated[str, Field(min_length=1, max_length=180)]
    size_bytes: Annotated[int, Field(ge=1)]
    sha256: Sha256
    source: SourceInput = Field(default_factory=SourceInput)
    purpose: Literal['library', 'processing'] = 'library'
    known_asset_id: Identifier | None = None

    @model_validator(mode='after')
    def filename_only(self):
        validate_relative_path(self.filename)
        if '/' in self.filename:
            raise ValueError('filename must be a display leaf name')
        return self


class UploadOptions(ContractModel):
    probe_max_pixels: Annotated[int, Field(ge=1, le=64*1024**2)] = 16*1024**2
    max_file_bytes: Annotated[int, Field(ge=1, le=2 * 1024**3)] = 2 * 1024**3
    chunk_bytes: Annotated[int, Field(ge=1, le=8 * 1024**2)] = 4 * 1024**2
    max_open_per_owner: Annotated[int, Field(ge=1, le=100)] = 8
    max_reserved_per_owner: Annotated[int, Field(ge=1)] = 4 * 1024**3
    finalize_seconds: Annotated[float, Field(gt=0, le=3600)] = 300.0
