from typing import Annotated
from pydantic import Field, model_validator
from media_tag.contracts.models import ContractModel, Identifier, Sha256


class AssetSelection(ContractModel):
    asset_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]
    source_sha256: Sha256


class GapSelection(ContractModel):
    gap_id: Identifier
    expected_revision: Annotated[int, Field(ge=1)]


class ExportSubmission(ContractModel):
    request_id: Identifier
    assets: Annotated[tuple[AssetSelection, ...], Field(max_length=500)] = ()
    gaps: Annotated[tuple[GapSelection, ...], Field(max_length=500)] = ()
    include_ai_drafts: bool = False
    include_confirmed_aliases: bool = False

    @model_validator(mode='after')
    def selected(self):
        if not self.assets and not self.gaps:raise ValueError('explicit selected records required')
        if len({a.asset_id for a in self.assets})!=len(self.assets) or len({g.gap_id for g in self.gaps})!=len(self.gaps):
            raise ValueError('duplicate export selection')
        return self
