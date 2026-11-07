"""Read the user-supplied contract without copying, normalizing or changing it."""

import csv
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path


REQUIRED_FILES = (
    "README.md",
    "素材库打标方案.md",
    "feishu_asset_index_fields.csv",
    "feishu_asset_index_template.csv",
    "feishu_gap_table_fields.csv",
    "feishu_gap_table_template.csv",
)
TEMPLATE_WIDTHS = {
    "feishu_asset_index_template.csv": 35,
    "feishu_gap_table_template.csv": 14,
}


class ContractInputError(ValueError):
    def __init__(self, code: str, message: str, missing_files: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.code = code
        self.missing_files = missing_files


@dataclass(frozen=True)
class CsvTable:
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class TemplateBundle:
    version: str
    fingerprint: str
    file_hashes: tuple[tuple[str, str], ...]
    tables: tuple[tuple[str, CsvTable], ...]

    def table(self, name: str) -> CsvTable:
        return dict(self.tables)[name]

    def summary(self) -> dict:
        return {
            "contract_version": self.version,
            "fingerprint": self.fingerprint,
            "file_sha256": dict(self.file_hashes),
            "template_columns": {name: len(self.table(name).header) for name in TEMPLATE_WIDTHS},
            "validation_scope": "file_presence_utf8_csv_shape_and_hash_only",
            "formal_semantics_verified": False,
        }


def read_local_file(root: Path, name: str) -> bytes:
    """Disallow symlinks escaping the input bundle; never write to input files."""
    root = root.resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root):
        raise ContractInputError("path_escape", "input file escapes its bundle directory")
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise ContractInputError("missing_input", f"missing input file: {name}", (name,)) from exc
    except OSError as exc:
        raise ContractInputError("unreadable_input", f"cannot read input file: {name}") from exc


def parse_csv(data: bytes, name: str, expected_width: int | None = None) -> CsvTable:
    try:
        text = data.decode("utf-8-sig")
        rows = list(csv.reader(StringIO(text, newline=""), strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise ContractInputError("invalid_csv", f"invalid UTF-8 CSV: {name}") from exc
    if not rows or not rows[0] or any(not value.strip() for value in rows[0]):
        raise ContractInputError("invalid_header", f"missing/blank column name: {name}")
    header = rows[0]
    if len(set(header)) != len(header):
        raise ContractInputError("invalid_header", f"duplicate column name: {name}")
    if expected_width is not None and len(header) != expected_width:
        raise ContractInputError("column_count", f"{name} requires {expected_width} columns")
    # Permit empty physical lines, but preserve every cell in actual records.
    records = tuple(tuple(row) for row in rows[1:] if row)
    if any(len(row) != len(header) for row in records):
        raise ContractInputError("row_width", f"inconsistent CSV record width: {name}")
    return CsvTable(tuple(header), records)


def load_template_bundle(directory: Path, *, contract_version: str) -> TemplateBundle:
    if not contract_version.strip():
        raise ContractInputError("missing_version", "an explicit contract version is required")
    missing = tuple(name for name in REQUIRED_FILES if not (directory / name).is_file())
    if missing:
        raise ContractInputError("missing_template", "original local contract files are missing", missing)
    hashes = []
    tables = []
    for name in REQUIRED_FILES:
        data = read_local_file(directory, name)
        hashes.append((name, sha256(data).hexdigest()))
        if name.endswith(".csv"):
            tables.append((name, parse_csv(data, name, TEMPLATE_WIDTHS.get(name))))
        else:
            try:
                if not data.decode("utf-8-sig").strip():
                    raise ValueError("empty document")
            except (UnicodeError, ValueError) as exc:
                raise ContractInputError("invalid_document", f"empty or invalid UTF-8 document: {name}") from exc
    fingerprint = sha256(json.dumps(dict(hashes), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return TemplateBundle(contract_version, fingerprint, tuple(hashes), tuple(tables))
