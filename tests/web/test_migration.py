from hashlib import sha256
import sqlite3
import pytest
from media_tag.catalog import Database, CatalogError
import media_tag.catalog.database as module


def test_v4_upgrade_rolls_back_on_failure_and_preserves_existing_state(tmp_path,monkeypatch):
    path=tmp_path/'synthetic-v4.sqlite3'
    connection=sqlite3.connect(path)
    for number,statements in [(1,module.SCHEMA),(2,module.ROUTER_SCHEMA),(3,module.UPLOAD_SCHEMA),(4,module.REDACTION_SCHEMA)]:
        for statement in statements:connection.execute(statement)
        connection.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(number,sha256('\n'.join(statements).encode()).hexdigest(),'synthetic'))
    connection.execute("INSERT INTO catalog_metadata VALUES('synthetic','retained')")
    connection.execute('PRAGMA user_version=4');connection.commit();connection.close()
    schema=module.LIBRARY_SCHEMA
    monkeypatch.setattr(module,'LIBRARY_SCHEMA',(*schema,'invalid synthetic SQL'))
    with pytest.raises(sqlite3.OperationalError):Database(path).initialize()
    with Database(path).transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==4
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='library_entries'").fetchone()
        assert c.execute('SELECT value FROM catalog_metadata').fetchone()[0]=='retained'
    monkeypatch.setattr(module,'LIBRARY_SCHEMA',schema)
    Database(path).initialize();Database(path).initialize()
    with Database(path).transaction(write=True) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==9
        assert c.execute('SELECT count(*) FROM schema_migrations').fetchone()[0]==9
        c.execute("UPDATE schema_migrations SET checksum='synthetic-tamper' WHERE version=5")
    with pytest.raises(CatalogError,match='checksum'):Database(path).initialize()
