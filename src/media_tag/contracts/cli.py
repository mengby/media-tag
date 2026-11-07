"""Local contract tools; does not start a service, create jobs, or publish data."""

import argparse
import json
from pathlib import Path
import sys

from pydantic import ValidationError

from .models import EffectiveValue, Evidence, FieldValue, TagSuggestion, TaskEnvelope, WorkerResult
from .templates import ContractInputError, load_template_bundle
from .vocabulary import VocabularyManifest, load_published_vocabulary

SCHEMAS = {
    "field-value": FieldValue,
    "suggestion": TagSuggestion,
    "effective-value": EffectiveValue,
    "evidence": Evidence,
    "task": TaskEnvelope,
    "result": WorkerResult,
    "vocabulary-manifest": VocabularyManifest,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="media-tag")
    commands = parser.add_subparsers(dest="command", required=True)
    contracts = commands.add_parser("contracts").add_subparsers(dest="action", required=True)
    template = contracts.add_parser("check-template")
    template.add_argument("--template-dir", type=Path, default=Path("asset-index-template"))
    template.add_argument("--contract-version", required=True)
    vocabulary = contracts.add_parser("check-vocabulary")
    vocabulary.add_argument("--manifest", type=Path, required=True)
    vocabulary.add_argument("--kind", choices=("taxonomy", "lexicon"), required=True)
    vocabulary.add_argument("--version", required=True)
    vocabulary.add_argument("--taxonomy-version")
    schema = contracts.add_parser("schema")
    schema.add_argument("--model", choices=tuple(SCHEMAS), required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "check-template":
            output = load_template_bundle(args.template_dir, contract_version=args.contract_version).summary()
        elif args.action == "check-vocabulary":
            manifest = load_published_vocabulary(
                args.manifest, expected_kind=args.kind, expected_version=args.version,
                expected_taxonomy_version=args.taxonomy_version,
            )
            output = {"kind": manifest.kind, "version": manifest.version, "content_sha256": manifest.content_sha256,
                      "validation_scope": "publication_metadata_version_hash_json_only", "semantic_review_verified": False}
        else:
            output = SCHEMAS[args.model].model_json_schema()
    except ContractInputError as exc:
        # ASCII-escaped JSON is valid UTF-8 even under a legacy Windows console code page.
        print(json.dumps({"error": exc.code, "message": str(exc), "missing_files": exc.missing_files}, ensure_ascii=True), file=sys.stderr)
        return 2
    except ValidationError as exc:
        # Do not echo user payloads, field values, or credentials into diagnostic output.
        print(json.dumps({"error": "invalid_contract", "issues": [
            {"location": item["loc"], "type": item["type"]} for item in exc.errors()
        ]}, ensure_ascii=True), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=True, indent=2))
    return 0
