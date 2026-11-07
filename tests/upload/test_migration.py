import sqlite3

import pytest

from media_tag.catalog import CatalogError, Database
import media_tag.catalog.database as module


def test_v2_to_v3_preserves_queue_and_atomic_failure(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic-v2.sqlite3'
    connection = sqlite3.connect(path)
    from hashlib import sha256
    for number, statements in [(1, module.SCHEMA), (2, module.ROUTER_SCHEMA)]:
        for sql in statements:
            connection.execute(sql)
        connection.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (number, sha256('\n'.join(statements).encode()).hexdigest(), 'synthetic-date'))
    connection.execute('UPDATE router_control SET paused=1')
    connection.execute('PRAGMA user_version=2'); connection.commit(); connection.close()
    database = Database(path)
    original = module.UPLOAD_SCHEMA
    monkeypatch.setattr(module, 'UPLOAD_SCHEMA', (*original, 'synthetic invalid SQL'))
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.transaction() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 2
        assert connection.execute('SELECT paused FROM router_control').fetchone()[0] == 1
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='uploads'").fetchone()
    monkeypatch.setattr(module, 'UPLOAD_SCHEMA', original)
    database.initialize()
    with database.transaction(write=True) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0] == 3
        assert connection.execute('SELECT paused FROM router_control').fetchone()[0] == 1
        connection.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=3")
    with pytest.raises(CatalogError, match='checksum'):
        database.initialize()
