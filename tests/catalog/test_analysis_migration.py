"""The all-media intent migration must not damage existing catalogue state."""

from hashlib import sha256
import sqlite3

import pytest

import media_tag.catalog.database as module


def test_v10_to_v11_failure_rolls_back_and_preserves_all_prior_checksums(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic-v10.sqlite3'
    schemas = (module.SCHEMA, module.ROUTER_SCHEMA, module.UPLOAD_SCHEMA, module.REDACTION_SCHEMA,
               module.LIBRARY_SCHEMA, module.REVIEW_SCHEMA, module.TERMINOLOGY_SCHEMA,
               module.TEXT_INDEX_SCHEMA, module.SEARCH_SCHEMA, module.DRAFT_SCHEMA)
    connection = sqlite3.connect(path)
    checksums = {}
    for number, statements in enumerate(schemas, 1):
        for statement in statements:
            connection.execute(statement)
        checksums[number] = sha256('\n'.join(statements).encode()).hexdigest()
        connection.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (number, checksums[number], 'synthetic'))
    connection.execute("INSERT INTO catalog_metadata VALUES('retained','unchanged')")
    connection.execute('UPDATE router_control SET paused=1')
    connection.execute('PRAGMA user_version=10')
    connection.commit(); connection.close()
    database = module.Database(path)
    original = module.ANALYSIS_SCHEMA
    monkeypatch.setattr(module, 'ANALYSIS_SCHEMA', (*original, 'invalid synthetic migration SQL'))
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 10
        assert dict(c.execute('SELECT version,checksum FROM schema_migrations')) == checksums
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='analysis_intents'").fetchone()
        assert c.execute('SELECT paused FROM router_control').fetchone()[0] == 1
    monkeypatch.setattr(module, 'ANALYSIS_SCHEMA', original)
    database.initialize(); database.initialize()
    with database.transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0] == 11
        assert dict(c.execute('SELECT version,checksum FROM schema_migrations WHERE version<=10')) == checksums
        assert c.execute("SELECT value FROM catalog_metadata WHERE key='retained'").fetchone()[0] == 'unchanged'
        assert c.execute('SELECT count(*) FROM analysis_intents').fetchone()[0] == 0
