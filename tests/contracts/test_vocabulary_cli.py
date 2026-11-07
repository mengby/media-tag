from hashlib import sha256
import json
import subprocess
import sys

import pytest
from pydantic import ValidationError

from media_tag.contracts.cli import SCHEMAS, main
from media_tag.contracts.templates import ContractInputError, REQUIRED_FILES
from media_tag.contracts.vocabulary import VocabularyManifest, load_published_vocabulary


def synthetic_release(root, **changes):
    content = b'{"synthetic_entries": []}'
    (root / "synthetic-content.json").write_bytes(content)
    manifest = {"kind": "taxonomy", "version": "synthetic-v1", "status": "published",
                "content_file": "synthetic-content.json", "content_sha256": sha256(content).hexdigest(),
                "published_by": "synthetic-publisher", "published_at": "2026-11-07T00:00:00Z"} | changes
    path = root / "synthetic-release.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_published_version_and_actual_hash_required(tmp_path):
    path = synthetic_release(tmp_path)
    manifest = load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v1")
    assert manifest.version == "synthetic-v1"
    with pytest.raises(ContractInputError) as error:
        load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v2")
    assert error.value.code == "vocabulary_version_mismatch"
    (tmp_path / "synthetic-content.json").write_bytes(b"{}")
    with pytest.raises(ContractInputError) as error:
        load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v1")
    assert error.value.code == "vocabulary_hash_mismatch"


@pytest.mark.parametrize("status", ["draft", "review", "rejected"])
def test_candidates_are_never_usable_as_published(tmp_path, status):
    path = synthetic_release(tmp_path, status=status)
    with pytest.raises(ContractInputError) as error:
        load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v1")
    assert error.value.code == "unpublished_vocabulary"


def test_lexicon_binds_expected_taxonomy(tmp_path):
    path = synthetic_release(tmp_path, kind="lexicon", taxonomy_version="synthetic-tax-v1")
    for expected in (None, "other"):
        with pytest.raises(ContractInputError):
            load_published_vocabulary(path, expected_kind="lexicon", expected_version="synthetic-v1", expected_taxonomy_version=expected)
    assert load_published_vocabulary(path, expected_kind="lexicon", expected_version="synthetic-v1", expected_taxonomy_version="synthetic-tax-v1").kind == "lexicon"
    with pytest.raises(ValidationError):
        VocabularyManifest.model_validate_json(path.read_text().replace('"synthetic-tax-v1"', "null"))


def test_missing_publisher_missing_content_and_path_escape(tmp_path):
    path = synthetic_release(tmp_path, published_by=None)
    with pytest.raises(ValidationError):
        load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v1")
    path = synthetic_release(tmp_path, content_file="../outside.json")
    with pytest.raises(ValidationError):
        load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v1")
    path = synthetic_release(tmp_path)
    (tmp_path / "synthetic-content.json").unlink()
    with pytest.raises(ContractInputError) as error:
        load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v1")
    assert error.value.code == "missing_input"


def test_duplicate_json_keys_rejected(tmp_path):
    path = synthetic_release(tmp_path)
    text = path.read_text().replace('"status": "published"', '"status": "draft", "status": "published"')
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ContractInputError) as error:
        load_published_vocabulary(path, expected_kind="taxonomy", expected_version="synthetic-v1")
    assert error.value.code == "invalid_json"


def test_cli_missing_template_is_structured_and_nonzero(tmp_path, capsys):
    assert main(["contracts", "check-template", "--template-dir", str(tmp_path), "--contract-version", "synthetic-v1"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["missing_files"] == list(REQUIRED_FILES)


@pytest.mark.parametrize("name", SCHEMAS)
def test_cli_schema_json(name, capsys):
    assert main(["contracts", "schema", "--model", name]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["additionalProperties"] is False


def test_cli_validation_error_does_not_echo_payload(tmp_path, capsys):
    path = synthetic_release(tmp_path, published_by="sensitive token with spaces")
    assert main(["contracts", "check-vocabulary", "--manifest", str(path), "--kind", "taxonomy", "--version", "synthetic-v1"]) == 2
    output = capsys.readouterr()
    assert "sensitive token" not in output.err
    assert json.loads(output.err)["error"] == "invalid_contract"


def test_real_cli_missing_chinese_filename_is_utf8_json(tmp_path):
    result = subprocess.run([sys.executable, "-m", "media_tag", "contracts", "check-template",
                             "--template-dir", str(tmp_path), "--contract-version", "synthetic-v1"],
                            capture_output=True)
    assert result.returncode == 2
    assert not result.stdout
    assert json.loads(result.stderr.decode("utf-8"))["missing_files"] == list(REQUIRED_FILES)
