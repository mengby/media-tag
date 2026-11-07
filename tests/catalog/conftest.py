from types import SimpleNamespace

import pytest

from media_tag.catalog import Catalog, Database, Principal
from media_tag.contracts.templates import FieldDefinition, TemplateBundle
from media_tag.storage.filesystem import FilesystemBackend


@pytest.fixture
def setup_catalog(tmp_path):
    originals = tmp_path / "synthetic-originals"
    objects = tmp_path / "synthetic-objects"
    originals.mkdir()
    objects.mkdir()
    fields = (
        FieldDefinition("synthetic_note", "文本", "synthetic", "人工", "human_only", None, None),
        FieldDefinition("synthetic_ai_text", "文本", "synthetic", "AI初稿+人工抽查", "ai_draft_human_sample_review", None, None),
        FieldDefinition("synthetic_script", "文本", "synthetic", "脚本", "script", None, None),
        FieldDefinition("synthetic_medical", "文本", "synthetic", "AI建议+人工逐条确认", "ai_candidate_per_field_human_review", None, None),
        FieldDefinition("synthetic_select", "单选", "锁定列表", "人工", "human_only", None, "external_vocabulary"),
    )
    # Synthetic in-memory contract only; this is not an accepted user template fingerprint.
    contract = TemplateBundle("synthetic-v1", "e" * 64, (), (), (("synthetic-template", fields),), True)
    database = Database(tmp_path / "synthetic.sqlite3")
    source = FilesystemBackend(originals)
    store = FilesystemBackend(objects, writable=True)
    catalog = Catalog(database, store, contract=contract)
    return SimpleNamespace(catalog=catalog, database=database, source=source, store=store, contract=contract,
                           originals=originals, objects=objects, actor=Principal(actor_id="synthetic-owner"))
