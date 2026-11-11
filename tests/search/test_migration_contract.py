from hashlib import sha256
import sqlite3
import pytest
from media_tag.catalog import Database
import media_tag.catalog.database as module
from media_tag.terminology.lexicon.validation import issues
from terminology.lexicon.conftest import taxonomy,option,concept,lexicon


def test_existing_purpose_options_and_free_process_text_do_not_expand_locked_enums(sought):
    good=taxonomy(option('purpose',field='用途大类',value=sought.purpose),option('process',field='过程/状态',value='合成图形增大→减小'))
    assert not issues(good,sought.env.contract)
    wrong=taxonomy(option('purpose',field='用途大类',value='新造的用途枚举'))
    assert any(i['code']=='locked_template_field' for i in issues(wrong,sought.env.contract))
    process=lexicon('synthetic-tax',concepts=[concept('process-concept',oid='process',concept_type='process')],aliases=[])
    assert not issues(process,sought.env.contract,good)


def test_v8_migration_failure_preserves_old_heads(tmp_path,monkeypatch):
    path=tmp_path/'v8.sqlite3';c=sqlite3.connect(path)
    migrations=[module.SCHEMA,module.ROUTER_SCHEMA,module.UPLOAD_SCHEMA,module.REDACTION_SCHEMA,module.LIBRARY_SCHEMA,module.REVIEW_SCHEMA,module.TERMINOLOGY_SCHEMA,module.TEXT_INDEX_SCHEMA]
    for version,statements in enumerate(migrations,1):
        for statement in statements:c.execute(statement)
        c.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(version,sha256('\n'.join(statements).encode()).hexdigest(),'synthetic'))
    c.execute('PRAGMA user_version=8');c.commit();c.close()
    valid=module.SEARCH_SCHEMA;monkeypatch.setattr(module,'SEARCH_SCHEMA',(*valid,'invalid SQL'))
    with pytest.raises(sqlite3.OperationalError):Database(path).initialize()
    with Database(path).transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==8
        assert c.execute('SELECT epoch FROM terminology_serving_head').fetchone()[0]==0
    monkeypatch.setattr(module,'SEARCH_SCHEMA',valid);Database(path).initialize()
    with Database(path).transaction() as c:assert c.execute('PRAGMA user_version').fetchone()[0]==11
