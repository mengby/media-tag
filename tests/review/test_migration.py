from hashlib import sha256
import sqlite3
import pytest
import media_tag.catalog.database as module
from media_tag.catalog import Database,CatalogError


def test_v5_to_v6_failure_rolls_back_then_preserves_old_data(tmp_path,monkeypatch):
    path=tmp_path/'synthetic-v5.sqlite3';c=sqlite3.connect(path)
    for version,statements in [(1,module.SCHEMA),(2,module.ROUTER_SCHEMA),(3,module.UPLOAD_SCHEMA),(4,module.REDACTION_SCHEMA),(5,module.LIBRARY_SCHEMA)]:
        for statement in statements:c.execute(statement)
        c.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(version,sha256('\n'.join(statements).encode()).hexdigest(),'synthetic'))
    c.execute("INSERT INTO catalog_metadata VALUES('synthetic','retained')");c.execute('PRAGMA user_version=5');c.commit();c.close()
    schema=module.REVIEW_SCHEMA;monkeypatch.setattr(module,'REVIEW_SCHEMA',(*schema,'invalid synthetic SQL'))
    with pytest.raises(sqlite3.OperationalError):Database(path).initialize()
    with Database(path).transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==5
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='review_observations'").fetchone()
    monkeypatch.setattr(module,'REVIEW_SCHEMA',schema);Database(path).initialize();Database(path).initialize()
    with Database(path).transaction(write=True) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==7
        assert c.execute('SELECT value FROM catalog_metadata').fetchone()[0]=='retained'
        c.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=6")
    with pytest.raises(CatalogError):Database(path).initialize()
