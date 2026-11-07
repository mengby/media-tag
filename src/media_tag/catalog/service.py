"""Transactional catalogue operations; trusted service layer, not an authentication API."""

from contextlib import contextmanager
from hashlib import sha256
import json
import os
import sqlite3
from typing import BinaryIO, Iterator, Literal, Mapping
from uuid import uuid4

from pydantic import TypeAdapter

from media_tag.contracts.models import EffectiveValue, Identifier, TagSuggestion, TaskEnvelope, Text, WorkerResult, validate_result_for_task
from media_tag.contracts.templates import TemplateBundle
from media_tag.storage import BackendNotConfigured, IntegrityError, StorageBackend, UnsafePath
from media_tag.storage.filesystem import FilesystemBackend
from .database import AccessDenied, CatalogError, Database, NotFound, RevisionConflict, UnsupportedRevision, utc_now
from .models import AssetSnapshot, LegacyAlias, Principal, SourceInput, SourceRecord


def encode(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def _identifier(value: str) -> str:
    return TypeAdapter(Identifier).validate_python(value, strict=True)


def _text(value: str) -> str:
    return TypeAdapter(Text).validate_python(value, strict=True)


def _human(principal: Principal) -> None:
    if principal.role not in ("editor", "admin"):
        raise AccessDenied("human editor capability required")


class Catalog:
    def __init__(self, database: Database, object_store: StorageBackend, *, contract: TemplateBundle,
                 backend_id: str = "local", read_backends: Mapping[str, StorageBackend] | None = None) -> None:
        if not contract.baseline_verified:
            raise CatalogError("catalogue requires an explicitly verified template baseline")
        self.database = database
        self.object_store = object_store
        self.backend_id = _identifier(backend_id)
        self.backends = dict(read_backends or {})
        self.backends[self.backend_id] = object_store
        self.fields = {field.name: field for field in contract.field_definitions[0][1]}
        self.database.initialize()
        with self.database.transaction(write=True) as connection:
            row = connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()
            if row is not None and row[0] != contract.fingerprint:
                raise CatalogError("catalogue is bound to a different template baseline; explicit migration required")
            connection.execute("INSERT OR IGNORE INTO catalog_metadata VALUES('template_fingerprint',?)", (contract.fingerprint,))

    @staticmethod
    def _authorize(connection: sqlite3.Connection, asset_id: str, principal: Principal, permission: str = "read") -> sqlite3.Row:
        row = connection.execute("SELECT * FROM assets WHERE asset_id=? AND deleted_at IS NULL", (asset_id,)).fetchone()
        if row is None:
            raise NotFound("asset is unavailable")
        allowed = principal.role == "admin" or row["owner_id"] == principal.actor_id
        if not allowed:
            permissions = {item[0] for item in connection.execute(
                "SELECT permission FROM asset_permissions WHERE asset_id=? AND subject_id=?", (asset_id, principal.actor_id))}
            allowed = permission in permissions or (permission == "read" and "edit" in permissions)
        if not allowed:
            raise AccessDenied("asset permission denied")
        return row

    @staticmethod
    def _expected(row: sqlite3.Row, revision: int) -> int:
        if type(revision) is not int or revision < 1 or revision != row["revision"]:
            raise RevisionConflict("asset revision changed; reload before applying changes")
        return revision + 1

    @staticmethod
    def _event(connection, asset_id, revision, actor, reason, before, after):
        connection.execute("INSERT INTO field_revisions VALUES(?,?,?,?,?,?,?)",
                           (asset_id, revision, actor.actor_id, _text(reason), encode(before), encode(after), utc_now()))

    def _snapshot(self, connection: sqlite3.Connection, row: sqlite3.Row) -> AssetSnapshot:
        asset_id = row["asset_id"]
        blob = connection.execute("SELECT size_bytes FROM blobs WHERE sha256=?", (row["blob_sha256"],)).fetchone()
        sources = tuple(SourceRecord(**dict(source)) for source in connection.execute(
            "SELECT source_id,label,reference,attribution,license_status,source_backend_id,source_key,recorded_by,created_at FROM asset_sources WHERE asset_id=? ORDER BY source_id", (asset_id,)))
        aliases = tuple(LegacyAlias(namespace=alias[0], alias=alias[1]) for alias in connection.execute(
            "SELECT namespace,alias FROM asset_aliases WHERE asset_id=? ORDER BY namespace,alias", (asset_id,)))
        effective = tuple(EffectiveValue.model_validate_json(item[0]) for item in connection.execute(
            "SELECT payload FROM effective_values WHERE asset_id=? ORDER BY field", (asset_id,)))
        suggestions = tuple(TagSuggestion.model_validate_json(item[0]) for item in connection.execute(
            "SELECT payload FROM tag_suggestions WHERE asset_id=? ORDER BY suggestion_id", (asset_id,)))
        return AssetSnapshot(asset_id, row["blob_sha256"], blob[0], row["owner_id"], row["name"], row["revision"], row["index_status"],
                             row["root_asset_id"], row["parent_asset_id"], row["created_at"], sources, aliases, effective, suggestions)

    def register_file(self, source_store: FilesystemBackend, source_key: str, *, source_backend_id: str, principal: Principal,
                      request_id: str, name: str, source: SourceInput | None = None,
                      aliases: tuple[LegacyAlias, ...] = ()) -> AssetSnapshot:
        _human(principal)
        _identifier(request_id)
        _identifier(source_backend_id)
        _text(name)
        source = source or SourceInput()
        if source_store.writable:
            raise UnsafePath("source backend must be read-only")
        if isinstance(self.object_store, FilesystemBackend):
            left, right = source_store.root, self.object_store.root
            if left.is_relative_to(right) or right.is_relative_to(left):
                raise UnsafePath("source and object roots must not overlap")
        alias_data = [alias.model_dump() for alias in aliases]
        if len({(alias.namespace, alias.alias) for alias in aliases}) != len(aliases):
            raise RevisionConflict("duplicate aliases in registration")
        with source_store.open_read(source_key) as stream:
            before = os.fstat(stream.fileno())
            blob = self.object_store.ingest(stream)
            after = os.fstat(stream.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev) != (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev) or blob.size_bytes != before.st_size:
                raise IntegrityError("source changed during registration; no catalogue record was published")
        fingerprint = sha256(encode({"sha256": blob.sha256, "size": blob.size_bytes, "name": name,
                                     "source_backend_id": source_backend_id, "source": source.model_dump(), "aliases": alias_data}).encode()).hexdigest()
        with self.database.transaction(write=True) as connection:
            previous = connection.execute("SELECT fingerprint,asset_id FROM registrations WHERE owner_id=? AND request_id=?",
                                          (principal.actor_id, request_id)).fetchone()
            if previous:
                if previous[0] != fingerprint:
                    raise RevisionConflict("registration request ID was reused with different input")
                return self._snapshot(connection, self._authorize(connection, previous[1], principal))
            for alias in aliases:
                if connection.execute("SELECT 1 FROM asset_aliases WHERE owner_id=? AND namespace=? AND alias=?",
                                      (principal.actor_id, alias.namespace, alias.alias)).fetchone():
                    raise RevisionConflict("legacy alias is already assigned")
            now, asset_id, source_id = utc_now(), f"asset-{uuid4().hex}", f"source-{uuid4().hex}"
            connection.execute("INSERT OR IGNORE INTO blobs VALUES(?,?,?)", (blob.sha256, blob.size_bytes, now))
            size = connection.execute("SELECT size_bytes FROM blobs WHERE sha256=?", (blob.sha256,)).fetchone()[0]
            if size != blob.size_bytes:
                raise IntegrityError("existing blob size differs from its registered hash")
            connection.execute("INSERT OR IGNORE INTO storage_locations VALUES(?,?,?)", (blob.sha256, self.backend_id, blob.object_key))
            location = connection.execute("SELECT object_key FROM storage_locations WHERE blob_sha256=? AND backend_id=?",
                                          (blob.sha256, self.backend_id)).fetchone()
            if location is None or location[0] != blob.object_key:
                raise IntegrityError("storage location conflicts with registered blob")
            connection.execute("""INSERT INTO assets(asset_id,blob_sha256,owner_id,name,revision,kind,root_asset_id,parent_asset_id,created_at,deleted_at)
                VALUES(?,?,?,?,1,'original',?,NULL,?,NULL)""",
                               (asset_id, blob.sha256, principal.actor_id, name, asset_id, now))
            connection.execute("INSERT INTO asset_sources VALUES(?,?,?,?,?,'unverified',?,?,?,?)",
                               (source_id, asset_id, source.label, source.reference, source.attribution,
                                source_backend_id, source_key, principal.actor_id, now))
            for alias in aliases:
                connection.execute("INSERT INTO asset_aliases VALUES(?,?,?,?)", (principal.actor_id, alias.namespace, alias.alias, asset_id))
            connection.execute("INSERT INTO registrations VALUES(?,?,?,?)", (principal.actor_id, request_id, fingerprint, asset_id))
            self._event(connection, asset_id, 1, principal, "register", {}, {"name": name, "source_id": source_id, "aliases": alias_data})
            return self._snapshot(connection, self._authorize(connection, asset_id, principal))

    def get_asset(self, asset_id: str, *, principal: Principal) -> AssetSnapshot:
        with self.database.transaction() as connection:
            return self._snapshot(connection, self._authorize(connection, asset_id, principal))

    def snapshot(self, *, principal: Principal) -> tuple[AssetSnapshot, ...]:
        """One read transaction: a detached, consistent view containing only visible assets."""
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT * FROM assets WHERE deleted_at IS NULL AND
                (? OR owner_id=? OR asset_id IN (SELECT asset_id FROM asset_permissions
                  WHERE subject_id=? AND permission IN ('read','edit'))) ORDER BY asset_id""",
                (principal.role == "admin", principal.actor_id, principal.actor_id)).fetchall()
            return tuple(self._snapshot(connection, row) for row in rows)

    def resolve_alias(self, namespace: str, alias: str, *, principal: Principal) -> AssetSnapshot:
        with self.database.transaction() as connection:
            rows = connection.execute("""SELECT assets.* FROM assets JOIN asset_aliases USING(asset_id)
                WHERE namespace=? AND alias=? AND deleted_at IS NULL AND
                (? OR assets.owner_id=? OR asset_id IN (SELECT asset_id FROM asset_permissions
                  WHERE subject_id=? AND permission IN ('read','edit')))""",
                (namespace, alias, principal.role == "admin", principal.actor_id, principal.actor_id)).fetchall()
            if not rows:
                raise NotFound("no visible asset matches the alias")
            if len(rows) != 1:
                raise RevisionConflict("alias matches multiple visible assets; use a fixed asset ID")
            return self._snapshot(connection, rows[0])

    def change_access(self, asset_id: str, *, principal: Principal, expected_revision: int,
                      subject_id: str, permission: Literal["read", "edit", "suggest"], reason: str,
                      revoke: bool = False) -> AssetSnapshot:
        _human(principal)
        _identifier(subject_id)
        if permission not in ("read", "edit", "suggest") or type(revoke) is not bool:
            raise ValueError("invalid access change")
        with self.database.transaction(write=True) as connection:
            row = self._authorize(connection, asset_id, principal)
            if row["owner_id"] != principal.actor_id and principal.role != "admin":
                raise AccessDenied("only the owner or administrator can change access")
            revision = self._expected(row, expected_revision)
            before = [list(item) for item in connection.execute("SELECT subject_id,permission FROM asset_permissions WHERE asset_id=? ORDER BY subject_id,permission", (asset_id,))]
            if revoke:
                connection.execute("DELETE FROM asset_permissions WHERE asset_id=? AND subject_id=? AND permission=?", (asset_id, subject_id, permission))
            else:
                connection.execute("INSERT OR IGNORE INTO asset_permissions VALUES(?,?,?)", (asset_id, subject_id, permission))
            after = [list(item) for item in connection.execute("SELECT subject_id,permission FROM asset_permissions WHERE asset_id=? ORDER BY subject_id,permission", (asset_id,))]
            connection.execute("UPDATE assets SET revision=? WHERE asset_id=?", (revision, asset_id))
            self._event(connection, asset_id, revision, principal, reason, {"permissions": before}, {"permissions": after})
            return self._snapshot(connection, self._authorize(connection, asset_id, principal))

    def revise(self, asset_id: str, *, principal: Principal, expected_revision: int, reason: str,
               name: str | None = None, values: tuple[EffectiveValue, ...] = ()) -> AssetSnapshot:
        _human(principal)
        if name is None and not values:
            raise ValueError("revision requires an explicit change")
        if name is not None:
            _text(name)
        if len({value.field for value in values}) != len(values):
            raise ValueError("duplicate revised fields")
        with self.database.transaction(write=True) as connection:
            row = self._authorize(connection, asset_id, principal, "edit")
            revision = self._expected(row, expected_revision)
            before = {"name": row["name"], "values": [json.loads(item[0]) for item in connection.execute(
                "SELECT payload FROM effective_values WHERE asset_id=? ORDER BY field", (asset_id,))]}
            for value in values:
                definition = self.fields.get(value.field)
                if definition is None or definition.field_type != "文本" or definition.fill_policy in ("script", "ai_candidate_per_field_human_review"):
                    raise UnsupportedRevision("D01 accepts non-medical text revisions only; other fields require D06/D13 validation")
                if value.actor_id != principal.actor_id or value.revision != revision:
                    raise RevisionConflict("effective value actor/revision does not match the authenticated change")
                connection.execute("""INSERT INTO effective_values VALUES(?,?,?) ON CONFLICT(asset_id,field)
                    DO UPDATE SET payload=excluded.payload""", (asset_id, value.field, value.model_dump_json()))
            connection.execute("UPDATE assets SET name=?,revision=? WHERE asset_id=?", (name if name is not None else row["name"], revision, asset_id))
            after = {"name": name if name is not None else row["name"], "values": [json.loads(item[0]) for item in connection.execute(
                "SELECT payload FROM effective_values WHERE asset_id=? ORDER BY field", (asset_id,))]}
            self._event(connection, asset_id, revision, principal, reason, before, after)
            return self._snapshot(connection, self._authorize(connection, asset_id, principal))

    def record_suggestions(self, task: TaskEnvelope, result: WorkerResult, *, principal: Principal) -> None:
        if principal.role != "worker":
            raise AccessDenied("worker identity required for suggestion records")
        validate_result_for_task(task, result)
        if result.status not in ("succeeded", "partial"):
            raise ValueError("only completed suggestion evidence can be recorded")
        with self.database.transaction(write=True) as connection:
            row = self._authorize(connection, task.asset_id, principal, "suggest")
            self._expected(row, task.asset_revision)
            if any(item.source_sha256 != row["blob_sha256"] for item in result.evidence):
                raise RevisionConflict("suggestion evidence does not reference the registered file version")
            task_json, result_json = task.model_dump_json(), result.model_dump_json()
            previous = connection.execute("""SELECT task_json,result_json FROM suggestion_runs
                WHERE asset_id=? AND task_id=? AND attempt=? AND lease_epoch=?""",
                (task.asset_id, task.task_id, task.attempt, task.lease_epoch)).fetchone()
            if previous is not None:
                if tuple(previous) != (task_json, result_json):
                    raise RevisionConflict("suggestion run was replayed with a different task or result")
                return None
            run_id = f"run-{uuid4().hex}"
            connection.execute("INSERT INTO suggestion_runs VALUES(?,?,?,?,?,?,?,?,?)",
                               (run_id, task.asset_id, task.task_id, task.attempt, task.lease_epoch,
                                task_json, result_json, principal.actor_id, utc_now()))
            for suggestion in result.suggestions:
                definition = self.fields.get(suggestion.field)
                if definition is None:
                    raise UnsupportedRevision("suggestion field is outside the bound template")
                if definition.fill_policy in ("human_only", "human_or_script") or (suggestion.origin == "ai" and definition.fill_policy == "script"):
                    raise UnsupportedRevision("worker suggestions cannot infer human-only or script-owned fields")
                if suggestion.value.state == "observed" and (definition.field_type in ("单选", "多选") or definition.fill_policy == "ai_candidate_per_field_human_review"):
                    raise UnsupportedRevision("observed medical/selection suggestions require D06/D13 vocabulary and evidence validation")
                payload = suggestion.model_dump_json()
                existing = connection.execute("SELECT 1 FROM tag_suggestions WHERE asset_id=? AND suggestion_id=?",
                                              (task.asset_id, suggestion.suggestion_id)).fetchone()
                if existing is not None:
                    raise RevisionConflict("suggestion ID was already recorded by another run; use a new ID")
                connection.execute("INSERT INTO tag_suggestions VALUES(?,?,?,?,?,?,?)",
                                   (task.asset_id, suggestion.suggestion_id, task.asset_revision, principal.actor_id, payload, run_id, utc_now()))
        # A suggest-only grant deliberately does not expose the asset's metadata or human values.
        return None

    def history(self, asset_id: str, *, principal: Principal) -> tuple[dict, ...]:
        with self.database.transaction() as connection:
            self._authorize(connection, asset_id, principal)
            return tuple(dict(row) for row in connection.execute("SELECT * FROM field_revisions WHERE asset_id=? ORDER BY revision", (asset_id,)))

    def archive(self, asset_id: str, *, principal: Principal, expected_revision: int, reason: str) -> None:
        _human(principal)
        with self.database.transaction(write=True) as connection:
            row = self._authorize(connection, asset_id, principal)
            if row["owner_id"] != principal.actor_id and principal.role != "admin":
                raise AccessDenied("only the owner or administrator can archive an asset")
            revision = self._expected(row, expected_revision)
            connection.execute("UPDATE assets SET deleted_at=?,revision=? WHERE asset_id=?", (utc_now(), revision, asset_id))
            self._event(connection, asset_id, revision, principal, reason, {"archived": False}, {"archived": True})

    @contextmanager
    def open_content(self, asset_id: str, *, principal: Principal) -> Iterator[BinaryIO]:
        with self.database.transaction() as connection:
            row = self._authorize(connection, asset_id, principal)
            if row["kind"] != "original":
                raise AccessDenied("derived-file serving requires the D09/output-review workflow")
            blob = connection.execute("SELECT size_bytes FROM blobs WHERE sha256=?", (row["blob_sha256"],)).fetchone()
            locations = connection.execute("""SELECT backend_id,object_key FROM storage_locations WHERE blob_sha256=?
                ORDER BY CASE WHEN backend_id=? THEN 0 ELSE 1 END,backend_id""", (row["blob_sha256"], self.backend_id)).fetchall()
            location = next((item for item in locations if item[0] in self.backends), None)
            if location is None:
                raise BackendNotConfigured("registered object backend is unavailable")
            digest, size, backend, key = row["blob_sha256"], blob[0], self.backends[location[0]], location[1]
        with backend.open_verified(key, digest, size) as stream:
            yield stream
