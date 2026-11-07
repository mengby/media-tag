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
class FieldDefinition:
    """Metadata loaded in memory; examples are deliberately not imported as data."""

    name: str
    field_type: str
    options_format: str
    fill_method: str
    fill_policy: str
    group: str | None
    selection_policy: str | None


@dataclass(frozen=True)
class TemplateBundle:
    version: str
    fingerprint: str
    file_hashes: tuple[tuple[str, str], ...]
    tables: tuple[tuple[str, CsvTable], ...]
    field_definitions: tuple[tuple[str, tuple[FieldDefinition, ...]], ...]
    baseline_verified: bool = False

    def table(self, name: str) -> CsvTable:
        return dict(self.tables)[name]

    def fields_for(self, template_name: str) -> tuple[FieldDefinition, ...]:
        return dict(self.field_definitions)[template_name]

    def summary(self) -> dict:
        asset_fields = {field.name: field for field in self.field_definitions[0][1]}
        return {
            "contract_version": self.version,
            "fingerprint": self.fingerprint,
            "file_sha256": dict(self.file_hashes),
            "template_columns": {name: len(self.table(name).header) for name in TEMPLATE_WIDTHS},
            "field_definition_counts": {name: len(fields) for name, fields in self.field_definitions},
            "incomplete_selection_definitions": {
                name: [field.name for field in fields if field.selection_policy == "external_vocabulary"
                       or (field.selection_policy == "shared_asset_options"
                           and asset_fields[field.name].selection_policy == "external_vocabulary")]
                for name, fields in self.field_definitions
            },
            "validation_scope": "file_presence_utf8_csv_field_order_metadata_and_hash",
            "field_definitions_verified": True,
            "baseline_verified": self.baseline_verified,
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


def parse_field_definitions(table: CsvTable, template: CsvTable, name: str) -> tuple[FieldDefinition, ...]:
    required = ("字段", "飞书字段类型", "选项/格式", "填写方式")
    if not all(column in table.header for column in required):
        raise ContractInputError("field_metadata_header", f"missing required metadata columns: {name}")
    records = [dict(zip(table.header, row, strict=True)) for row in table.rows]
    if tuple(row["字段"] for row in records) != template.header:
        raise ContractInputError("field_order_mismatch", f"field definitions must match template columns in order: {name}")
    policies = {
        "人工": "human_only", "人工/脚本": "human_or_script", "人工/AI": "ai_candidate_human_review",
        "AI初稿+人工抽查": "ai_draft_human_sample_review", "AI建议+人工逐条确认": "ai_candidate_per_field_human_review",
    }
    field_types = {"文本（主字段）", "文本", "超链接", "附件", "单选", "多选", "数字", "复选框", "日期", "人员", "关联"}
    fields = []
    for row in records:
        kind, method, options = row["飞书字段类型"], row["填写方式"], row["选项/格式"]
        if kind not in field_types:
            raise ContractInputError("unknown_field_type", f"unsupported field metadata type: {name}")
        policy = policies.get(method)
        if method == "脚本" or (method.startswith("脚本(") and method.endswith(")")):
            policy = "script"
        if policy is None:
            raise ContractInputError("unknown_fill_method", f"unrecognized field ownership rule: {name}")
        selection_policy = None
        if kind in ("单选", "多选"):
            if not options.strip():
                raise ContractInputError("missing_selection_rule", f"selection field is missing its rule: {name}")
            if options == "选项同素材表":
                selection_policy = "shared_asset_options"
            elif any(marker in options for marker in ("锁定列表", "…", "...")):
                selection_policy = "external_vocabulary"
            elif options == "job_id":
                selection_policy = "dynamic_ids"
            else:
                selection_policy = "declared_format"
        fields.append(FieldDefinition(row["字段"], kind, options, method, policy, row.get("分组"), selection_policy))
    return tuple(fields)


def load_template_bundle(
    directory: Path, *, contract_version: str | None = None, expected_fingerprint: str | None = None,
) -> TemplateBundle:
    if contract_version is not None and not contract_version.strip():
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
    if expected_fingerprint is not None and fingerprint != expected_fingerprint:
        raise ContractInputError("template_baseline_mismatch", "template bytes differ from the expected contract baseline")
    table_map = dict(tables)
    definitions = []
    for template_name in TEMPLATE_WIDTHS:
        fields_name = template_name.replace("_template.csv", "_fields.csv")
        definitions.append((template_name, parse_field_definitions(table_map[fields_name], table_map[template_name], fields_name)))
    asset_fields = {field.name: field for field in definitions[0][1]}
    for field in definitions[1][1]:
        if field.selection_policy == "shared_asset_options":
            source = asset_fields.get(field.name)
            if source is None or source.field_type != field.field_type:
                raise ContractInputError("invalid_shared_options", "shared field is missing or has an incompatible type")
    return TemplateBundle(contract_version or f"sha256:{fingerprint}", fingerprint, tuple(hashes), tuple(tables),
                          tuple(definitions), expected_fingerprint is not None)
