"""Trusted caller identity and detached, authorized snapshots."""

from dataclasses import dataclass
from typing import Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import model_validator

from media_tag.contracts.models import ContractModel, EffectiveValue, Identifier, TagSuggestion, Text


class Principal(ContractModel):
    actor_id: Identifier
    role: Literal["editor", "worker", "admin", "reader"] = "editor"


class LegacyAlias(ContractModel):
    namespace: Text
    alias: Text


class SourceInput(ContractModel):
    label: Text | None = None
    reference: Text | None = None
    attribution: Text | None = None
    license_status: Literal["unverified"] = "unverified"

    @model_validator(mode="after")
    def check_reference(self) -> Self:
        if self.reference:
            url = urlsplit(self.reference)
            keys = {key.lower() for key, _ in parse_qsl(url.query)}
            if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password:
                raise ValueError("source reference must be an HTTP(S) page without credentials")
            if any(key in {"token", "signature", "access_token", "sig"} or key.startswith(("x-amz-", "x-oss-")) for key in keys):
                raise ValueError("temporary signed/token URLs cannot be persisted as source pages")
        return self


@dataclass(frozen=True)
class SourceRecord:
    source_id: str
    label: str | None
    reference: str | None
    attribution: str | None
    license_status: str
    source_backend_id: str
    source_key: str
    recorded_by: str
    created_at: str


@dataclass(frozen=True)
class AssetSnapshot:
    asset_id: str
    sha256: str
    size_bytes: int
    owner_id: str
    name: str
    revision: int
    index_status: str
    root_asset_id: str
    parent_asset_id: str | None
    created_at: str
    sources: tuple[SourceRecord, ...]
    aliases: tuple[LegacyAlias, ...]
    effective_values: tuple[EffectiveValue, ...]
    suggestions: tuple[TagSuggestion, ...]
