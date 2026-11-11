import sqlite3

import pytest

from media_tag.catalog import CatalogError, Database
import media_tag.catalog.database as database_module
from media_tag.storage import UnsafePath


def test_migration_is_repeatable_enforces_foreign_keys_and_checksum(tmp_path):
    database = Database(tmp_path / "synthetic.sqlite3")
    database.initialize()
    database.initialize()
    with database.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 11
    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction(write=True) as connection:
            connection.execute("INSERT INTO storage_locations VALUES(?,?,?)", ("a" * 64, "synthetic", "synthetic.bin"))
    with database.transaction(write=True) as connection:
        connection.execute("UPDATE schema_migrations SET checksum='synthetic-tamper'")
    with pytest.raises(CatalogError, match="checksum"):
        database.initialize()


def test_migration_failure_rolls_back_ddl_and_version(tmp_path, monkeypatch):
    database = Database(tmp_path / "synthetic.sqlite3")
    valid = database_module.SCHEMA
    monkeypatch.setattr(database_module, "SCHEMA", (*valid, "synthetic invalid SQL"))
    with pytest.raises(sqlite3.OperationalError):
        database.initialize()
    with database.transaction() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0] == 0
    monkeypatch.setattr(database_module, "SCHEMA", valid)
    database.initialize()


def test_future_schema_and_unknown_existing_database_are_not_adopted(tmp_path):
    path = tmp_path / "synthetic-future.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version=99")
    connection.close()
    with pytest.raises(CatalogError, match="unsupported"):
        Database(path).initialize()
    other = tmp_path / "synthetic-existing.sqlite3"
    connection = sqlite3.connect(other)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    connection.execute("INSERT INTO unrelated VALUES('synthetic')")
    connection.commit()
    connection.close()
    with pytest.raises(CatalogError, match="unversioned"):
        Database(other).initialize()
    connection = sqlite3.connect(other)
    assert connection.execute("SELECT value FROM unrelated").fetchone()[0] == "synthetic"
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    connection.close()


def test_database_rejects_network_path():
    with pytest.raises(UnsafePath):
        Database("\\\\synthetic-server\\synthetic-share\\catalog.sqlite3")
