"""Published vocabulary entry gate, not a vocabulary publisher or medical validator."""

from hashlib import sha256
import json
from pathlib import Path
from typing import Literal, Self

from pydantic import AwareDatetime, model_validator

from . import SCHEMA_VERSION
from .models import ContractModel, Identifier, Sha256, Text, validate_relative_path
from .templates import ContractInputError, read_local_file


def parse_json(data: bytes) -> object:
    def unique_keys(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid_constant(value: str) -> None:
        raise ValueError("non-finite JSON number")

    try:
        return json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_keys, parse_constant=invalid_constant)
    except (UnicodeError, ValueError) as exc:
        raise ContractInputError("invalid_json", "invalid JSON input") from exc


class VocabularyManifest(ContractModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    kind: Literal["taxonomy", "lexicon"]
    version: Text
    status: Literal["draft", "review", "published", "rejected"] = "draft"
    content_file: Text
    content_sha256: Sha256
    taxonomy_version: Text | None = None
    published_by: Identifier | None = None
    published_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_release_metadata(self) -> Self:
        validate_relative_path(self.content_file)
        if self.kind == "lexicon" and self.taxonomy_version is None:
            raise ValueError("lexicon must bind a taxonomy version")
        if self.status == "published" and (self.published_by is None or self.published_at is None):
            raise ValueError("published vocabulary requires publisher and timestamp")
        return self


def load_published_vocabulary(
    manifest_path: Path, *, expected_kind: Literal["taxonomy", "lexicon"], expected_version: str,
    expected_taxonomy_version: str | None = None,
) -> VocabularyManifest:
    data = read_local_file(manifest_path.parent, manifest_path.name)
    # Re-serialize after strict JSON syntax/duplicate-key checking to retain JSON-mode datetime validation.
    manifest = VocabularyManifest.model_validate_json(json.dumps(parse_json(data)))
    if manifest.status != "published":
        raise ContractInputError("unpublished_vocabulary", "draft/review/rejected vocabulary cannot be used as published")
    if (manifest.kind, manifest.version) != (expected_kind, expected_version):
        raise ContractInputError("vocabulary_version_mismatch", "vocabulary kind/version mismatch")
    if expected_kind == "lexicon":
        if not expected_taxonomy_version:
            raise ContractInputError("missing_taxonomy_version", "lexicon use requires the expected taxonomy version")
        if manifest.taxonomy_version != expected_taxonomy_version:
            raise ContractInputError("taxonomy_version_mismatch", "lexicon taxonomy version mismatch")
    content = read_local_file(manifest_path.parent, manifest.content_file)
    if sha256(content).hexdigest() != manifest.content_sha256:
        raise ContractInputError("vocabulary_hash_mismatch", "vocabulary content hash mismatch")
    payload = parse_json(content)
    if not isinstance(payload, dict):
        raise ContractInputError("invalid_vocabulary", "vocabulary content must be a JSON object")
    return manifest
