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
            width = TEMPLATE_WIDTHS.get(name, 3)
            stream = StringIO(newline="")
            writer = csv.writer(stream)
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
