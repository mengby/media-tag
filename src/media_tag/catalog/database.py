"""Versioned, local-only SQLite state with explicit transaction boundaries."""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import sqlite3
from typing import Iterator

from media_tag.storage.filesystem import require_local_path
from media_tag.router.schema import ROUTER_SCHEMA
from media_tag.upload.schema import UPLOAD_SCHEMA


class CatalogError(Exception):
    pass


class AccessDenied(CatalogError):
    pass


class NotFound(CatalogError):
    pass


class RevisionConflict(CatalogError):
    pass


class UnsupportedRevision(CatalogError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


SCHEMA = (
    "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)",
    "CREATE TABLE catalog_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE blobs(
        sha256 TEXT PRIMARY KEY CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size_bytes INTEGER NOT NULL CHECK(size_bytes>=0), created_at TEXT NOT NULL)""",
    """CREATE TABLE assets(
        asset_id TEXT PRIMARY KEY, blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
        owner_id TEXT NOT NULL, name TEXT NOT NULL CHECK(length(trim(name))>0),
        revision INTEGER NOT NULL CHECK(revision>=1), kind TEXT NOT NULL CHECK(kind IN ('original','derived')),
        index_status TEXT NOT NULL DEFAULT 'ai_draft' CHECK(index_status='ai_draft'),
        root_asset_id TEXT NOT NULL REFERENCES assets(asset_id), parent_asset_id TEXT REFERENCES assets(asset_id),
        created_at TEXT NOT NULL, deleted_at TEXT,
        CHECK((kind='original' AND root_asset_id=asset_id AND parent_asset_id IS NULL)
           OR (kind='derived' AND parent_asset_id IS NOT NULL)))""",
    """CREATE TRIGGER immutable_asset_identity BEFORE UPDATE OF asset_id,blob_sha256,owner_id,kind,root_asset_id,parent_asset_id ON assets
        BEGIN SELECT RAISE(ABORT, 'asset content and identity are immutable'); END""",
    """CREATE TRIGGER immutable_blob BEFORE UPDATE ON blobs
        BEGIN SELECT RAISE(ABORT, 'blob identity is immutable'); END""",
    """CREATE TABLE asset_sources(
        source_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id),
        label TEXT, reference TEXT, attribution TEXT, license_status TEXT NOT NULL CHECK(license_status='unverified'),
        source_backend_id TEXT NOT NULL, source_key TEXT NOT NULL, recorded_by TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE storage_locations(
        blob_sha256 TEXT NOT NULL REFERENCES blobs(sha256), backend_id TEXT NOT NULL, object_key TEXT NOT NULL,
        PRIMARY KEY(blob_sha256,backend_id), UNIQUE(backend_id,object_key))""",
    """CREATE TABLE asset_aliases(
        owner_id TEXT NOT NULL, namespace TEXT NOT NULL, alias TEXT NOT NULL,
        asset_id TEXT NOT NULL REFERENCES assets(asset_id), PRIMARY KEY(owner_id,namespace,alias))""",
    """CREATE TABLE registrations(
        owner_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        asset_id TEXT NOT NULL REFERENCES assets(asset_id), PRIMARY KEY(owner_id,request_id))""",
    """CREATE TABLE asset_permissions(
        asset_id TEXT NOT NULL REFERENCES assets(asset_id), subject_id TEXT NOT NULL,
        permission TEXT NOT NULL CHECK(permission IN ('read','edit','suggest')),
        PRIMARY KEY(asset_id,subject_id,permission))""",
    """CREATE TABLE effective_values(
        asset_id TEXT NOT NULL REFERENCES assets(asset_id), field TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(asset_id,field))""",
    """CREATE TABLE suggestion_runs(
        run_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES assets(asset_id), task_id TEXT NOT NULL,
        attempt INTEGER NOT NULL, lease_epoch INTEGER NOT NULL, task_json TEXT NOT NULL, result_json TEXT NOT NULL,
        actor_id TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(asset_id,task_id,attempt,lease_epoch))""",
    """CREATE TABLE tag_suggestions(
        asset_id TEXT NOT NULL REFERENCES assets(asset_id), suggestion_id TEXT NOT NULL,
        base_revision INTEGER NOT NULL, actor_id TEXT NOT NULL, payload TEXT NOT NULL,
        run_id TEXT NOT NULL REFERENCES suggestion_runs(run_id), created_at TEXT NOT NULL, PRIMARY KEY(asset_id,suggestion_id))""",
    """CREATE TABLE field_revisions(
        asset_id TEXT NOT NULL REFERENCES assets(asset_id), revision INTEGER NOT NULL,
        actor_id TEXT NOT NULL, reason TEXT NOT NULL, before_json TEXT NOT NULL, after_json TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(asset_id,revision))""",
    "CREATE INDEX assets_blob ON assets(blob_sha256)",
    "CREATE INDEX permissions_subject ON asset_permissions(subject_id,asset_id)",
)
SCHEMA_CHECKSUM = sha256("\n".join(SCHEMA).encode()).hexdigest()


class Database:
    def __init__(self, path: Path) -> None:
        require_local_path(Path(path))
        self.path = Path(path).resolve()
        require_local_path(self.path)
        if not self.path.parent.is_dir():
            raise CatalogError("database parent directory must already exist")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        migrations = ((1, SCHEMA, SCHEMA_CHECKSUM), (2, ROUTER_SCHEMA, sha256("\n".join(ROUTER_SCHEMA).encode()).hexdigest()),
                      (3, UPLOAD_SCHEMA, sha256("\n".join(UPLOAD_SCHEMA).encode()).hexdigest()))
        connection = self._connect()
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise CatalogError("unsupported database schema version")
            if version == 0 and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                raise CatalogError("refusing to initialize an unversioned, nonempty database")
            connection.execute("PRAGMA journal_mode=WAL")
        finally:
            connection.close()
        with self.transaction(write=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                    raise CatalogError("refusing to initialize an unversioned, nonempty database")
                # Do not use executescript: it can implicitly commit outside our migration transaction.
                for statement in SCHEMA:
                    connection.execute(statement)
                connection.execute("INSERT INTO schema_migrations VALUES(1,?,?)", (SCHEMA_CHECKSUM, utc_now()))
                connection.execute("PRAGMA user_version=1")
                version = 1
            if version not in (1, 2, 3):
                raise CatalogError("unsupported database schema version")
            for number, statements, checksum in migrations:
                if number > version:
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_migrations VALUES(?,?,?)", (number, checksum, utc_now()))
                    connection.execute(f"PRAGMA user_version={number}")
                row = connection.execute("SELECT checksum FROM schema_migrations WHERE version=?", (number,)).fetchone()
                if row is None or row[0] != checksum:
                    raise CatalogError("database migration checksum mismatch")
