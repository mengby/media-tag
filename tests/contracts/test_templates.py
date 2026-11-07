import csv
from hashlib import sha256
from io import StringIO

import pytest

from media_tag.contracts.templates import (
    ContractInputError, REQUIRED_FILES, TEMPLATE_WIDTHS, load_template_bundle, parse_csv, read_local_file,
)


def synthetic_bundle(root):
    root.mkdir(exist_ok=True)
    for name in REQUIRED_FILES:
        if name.endswith(".csv"):
            template_name = name.replace("_fields.csv", "_template.csv")
            width = TEMPLATE_WIDTHS[template_name]
            stream = StringIO(newline="")
            writer = csv.writer(stream)
            if name.endswith("_fields.csv"):
                writer.writerow(["字段", "飞书字段类型", "选项/格式", "填写方式", "synthetic_example"])
                writer.writerows([[f"synthetic_{i}", "文本", "synthetic_format", "脚本", "synthetic_example"] for i in range(width)])
            else:
                writer.writerow([f"synthetic_{i}" for i in range(width)])
                writer.writerow(['synthetic,含逗号\n换行"引用'] + [""] * (width - 1))
            (root / name).write_bytes(b"\xef\xbb\xbf" + stream.getvalue().encode())
        else:
            (root / name).write_text("Synthetic test rules, not a formal contract.", encoding="utf-8")
    return root


def test_missing_lists_every_required_file_without_creating_directory(tmp_path):
    root = tmp_path / "absent"
    with pytest.raises(ContractInputError) as error:
        load_template_bundle(root, contract_version="synthetic-v1")
    assert error.value.code == "missing_template"
    assert error.value.missing_files == REQUIRED_FILES
    assert not root.exists()


def test_read_only_preserves_order_bom_crlf_and_hashes(tmp_path):
    root = synthetic_bundle(tmp_path)
    before = {p.name: p.read_bytes() for p in root.iterdir()}
    bundle = load_template_bundle(root, contract_version="synthetic-v1")
    assert bundle.table("feishu_asset_index_template.csv").header == tuple(f"synthetic_{i}" for i in range(35))
    assert bundle.table("feishu_gap_table_template.csv").header == tuple(f"synthetic_{i}" for i in range(14))
    assert bundle.table("feishu_asset_index_template.csv").rows[0][0] == 'synthetic,含逗号\n换行"引用'
    assert dict(bundle.file_hashes) == {name: sha256(data).hexdigest() for name, data in before.items()}
    assert {p.name: p.read_bytes() for p in root.iterdir()} == before
    assert bundle.summary()["formal_semantics_verified"] is False
    assert "synthetic_0" not in str(bundle.summary())


def test_fingerprint_tracks_rules_and_column_order(tmp_path):
    root = synthetic_bundle(tmp_path)
    original = load_template_bundle(root, contract_version="synthetic-v1")
    (root / "README.md").write_text("changed synthetic rules", encoding="utf-8")
    changed = load_template_bundle(root, contract_version="synthetic-v1")
    assert original.fingerprint != changed.fingerprint
    name = "feishu_gap_table_template.csv"
    raw = (root / name).read_bytes().replace(b"synthetic_0,synthetic_1,", b"synthetic_1,synthetic_0,")
    (root / name).write_bytes(raw)
    fields_path = root / name.replace("_template.csv", "_fields.csv")
    table = parse_csv(fields_path.read_bytes(), fields_path.name)
    rewrite_csv(fields_path, [table.header, table.rows[1], table.rows[0], *table.rows[2:]])
    reordered = load_template_bundle(root, contract_version="synthetic-v1")
    assert reordered.fingerprint != changed.fingerprint
    assert reordered.table(name).header[:2] == ("synthetic_1", "synthetic_0")


@pytest.mark.parametrize("data,code", [
    (b"", "invalid_header"), (b"a,a\r\n", "invalid_header"),
    (b"a, \r\n", "invalid_header"), (b"a,b\r\nx\r\n", "row_width"),
    (b'a\r\n"unterminated', "invalid_csv"), (b"\xff", "invalid_csv"),
])
def test_invalid_csv(data, code):
    with pytest.raises(ContractInputError) as error:
        parse_csv(data, "synthetic.csv")
    assert error.value.code == code


def test_wrong_width_missing_file_and_empty_rules(tmp_path):
    root = synthetic_bundle(tmp_path)
    name = "feishu_asset_index_template.csv"
    (root / name).write_bytes(b"synthetic_only\r\n")
    with pytest.raises(ContractInputError, match="35 columns"):
        load_template_bundle(root, contract_version="synthetic-v1")
    (root / name).unlink()
    with pytest.raises(ContractInputError) as error:
        load_template_bundle(root, contract_version="synthetic-v1")
    assert error.value.missing_files == (name,)
    synthetic_bundle(root)
    (root / "README.md").write_bytes(b" ")
    with pytest.raises(ContractInputError) as error:
        load_template_bundle(root, contract_version="synthetic-v1")
    assert error.value.code == "invalid_document"


def test_version_required(tmp_path):
    with pytest.raises(ContractInputError, match="version"):
        load_template_bundle(tmp_path, contract_version=" ")


def test_bundle_file_must_stay_in_root(tmp_path):
    with pytest.raises(ContractInputError) as error:
        read_local_file(tmp_path, "../outside.csv")
    assert error.value.code == "path_escape"


def rewrite_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        csv.writer(stream).writerows(rows)


def test_content_identity_and_accepted_baseline_detect_drift(tmp_path):
    root = synthetic_bundle(tmp_path)
    original = load_template_bundle(root)
    assert original.version == f"sha256:{original.fingerprint}"
    verified = load_template_bundle(root, expected_fingerprint=original.fingerprint)
    assert verified.summary()["baseline_verified"] is True
    (root / "README.md").write_text("changed synthetic rules", encoding="utf-8")
    with pytest.raises(ContractInputError) as error:
        load_template_bundle(root, expected_fingerprint=original.fingerprint)
    assert error.value.code == "template_baseline_mismatch"


@pytest.mark.parametrize("change,code", [
    ("reorder", "field_order_mismatch"), ("missing_row", "field_order_mismatch"),
    ("duplicate_field", "field_order_mismatch"), ("rename_metadata", "field_metadata_header"),
    ("unknown_type", "unknown_field_type"), ("unknown_owner", "unknown_fill_method"),
    ("empty_selection", "missing_selection_rule"),
])
def test_invalid_field_definitions(tmp_path, change, code):
    root = synthetic_bundle(tmp_path)
    name = "feishu_asset_index_fields.csv"
    table = parse_csv((root / name).read_bytes(), name)
    rows = [list(table.header), *(list(row) for row in table.rows)]
    if change == "reorder":
        rows[1], rows[2] = rows[2], rows[1]
    elif change == "missing_row":
        rows.pop()
    elif change == "duplicate_field":
        rows[2][0] = rows[1][0]
    elif change == "rename_metadata":
        rows[0][0] = "synthetic_other"
    elif change == "unknown_type":
        rows[1][1] = "synthetic_unsupported"
    elif change == "unknown_owner":
        rows[1][3] = "synthetic_unsupported"
    elif change == "empty_selection":
        rows[1][1:3] = ["单选", ""]
    rewrite_csv(root / name, rows)
    with pytest.raises(ContractInputError) as error:
        load_template_bundle(root)
    assert error.value.code == code


@pytest.mark.parametrize("method,policy", [
    ("AI建议+人工逐条确认", "ai_candidate_per_field_human_review"),
    ("人工", "human_only"), ("AI初稿+人工抽查", "ai_draft_human_sample_review"),
])
def test_ownership_and_incomplete_options_are_metadata_not_a_release(tmp_path, method, policy):
    root = synthetic_bundle(tmp_path)
    name = "feishu_asset_index_fields.csv"
    table = parse_csv((root / name).read_bytes(), name)
    rows = [list(table.header), *(list(row) for row in table.rows)]
    rows[1][1:4] = ["多选", "锁定列表", method]
    rewrite_csv(root / name, rows)
    bundle = load_template_bundle(root)
    field = bundle.fields_for("feishu_asset_index_template.csv")[0]
    assert field.fill_policy == policy
    assert field.options_format == "锁定列表"
    assert field.selection_policy == "external_vocabulary"
    assert bundle.summary()["incomplete_selection_definitions"]["feishu_asset_index_template.csv"] == ["synthetic_0"]
    assert bundle.summary()["formal_semantics_verified"] is False


def test_shared_selection_requires_same_field_type(tmp_path):
    root = synthetic_bundle(tmp_path)
    for name, options in [("feishu_asset_index_fields.csv", "synthetic_a|synthetic_b"),
                          ("feishu_gap_table_fields.csv", "选项同素材表")]:
        table = parse_csv((root / name).read_bytes(), name)
        rows = [list(table.header), *(list(row) for row in table.rows)]
        rows[1][1:3] = ["单选", options]
        rewrite_csv(root / name, rows)
    bundle = load_template_bundle(root)
    assert bundle.fields_for("feishu_gap_table_template.csv")[0].selection_policy == "shared_asset_options"
    name = "feishu_asset_index_fields.csv"
    table = parse_csv((root / name).read_bytes(), name)
    rows = [list(table.header), *(list(row) for row in table.rows)]
    rows[1][2] = "锁定列表"
    rewrite_csv(root / name, rows)
    incomplete = load_template_bundle(root).summary()["incomplete_selection_definitions"]
    assert incomplete["feishu_gap_table_template.csv"] == ["synthetic_0"]
    rows[1][1] = "多选"
    rewrite_csv(root / name, rows)
    with pytest.raises(ContractInputError) as error:
        load_template_bundle(root)
    assert error.value.code == "invalid_shared_options"
