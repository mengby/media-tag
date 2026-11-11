"""Opt-in dispatch configuration; model availability is never inferred here."""
from pydantic import Field

from media_tag.contracts.models import ContractModel


class AnalysisDeployment(ContractModel):
    enabled: bool = False
    dispatch_limit: int = Field(default=32, ge=1, le=256)
    probe_max_pixels: int = Field(default=16*1024**2, ge=1, le=64*1024**2)
