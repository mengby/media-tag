from hashlib import sha256
import sqlite3

import pytest

import media_tag.catalog.database as module


def test_v12_failure_is_transactional_and_all_v1_to_v11_checksums_remain_identical(tmp_path,monkeypatch):
    path=tmp_path/'synthetic-v11.sqlite3'
    schemas=(module.SCHEMA,module.ROUTER_SCHEMA,module.UPLOAD_SCHEMA,module.REDACTION_SCHEMA,module.LIBRARY_SCHEMA,
        module.REVIEW_SCHEMA,module.TERMINOLOGY_SCHEMA,module.TEXT_INDEX_SCHEMA,module.SEARCH_SCHEMA,module.DRAFT_SCHEMA,module.ANALYSIS_SCHEMA)
    checksums={}
    with sqlite3.connect(path) as connection:
        for number,statements in enumerate(schemas,1):
            for statement in statements:connection.execute(statement)
            digest=sha256('\n'.join(statements).encode()).hexdigest();checksums[number]=digest
            connection.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(number,digest,'synthetic'))
        connection.execute("INSERT INTO catalog_metadata VALUES('retained','original')")
        connection.execute('PRAGMA user_version=11')
    database=module.Database(path);actual=module.PROCESSING_SCHEMA
    monkeypatch.setattr(module,'PROCESSING_SCHEMA',(*actual,'invalid synthetic SQL'))
    with pytest.raises(sqlite3.OperationalError):database.initialize()
    with database.transaction() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==11
        assert dict(connection.execute('SELECT version,checksum FROM schema_migrations'))==checksums
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='processing_derivations'").fetchone() is None
    monkeypatch.setattr(module,'PROCESSING_SCHEMA',actual);database.initialize();database.initialize()
    with database.transaction() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==12
        assert dict(connection.execute('SELECT version,checksum FROM schema_migrations WHERE version<=11'))==checksums
        assert connection.execute("SELECT value FROM catalog_metadata WHERE key='retained'").fetchone()[0]=='original'
