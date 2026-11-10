"""Build a portable, selected catalogue from one read-only SQLite snapshot."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import re
from types import MethodType, SimpleNamespace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field as PField

from media_contracts import Asset, Blob, Catalog as PublishedCatalog, Evidence, EvidencePayload, Field, Manifest, Preview, Source, Technical, canonical
from media_tag.catalog import Catalog, Principal
from media_tag.contracts.models import TaskEnvelope, WorkerResult
from media_tag.drafts.service import DraftIndex
from media_tag.files.service import FileService
from media_tag.review.policy import MEDICAL
from media_tag.router.service import Router
from media_tag.search.service import SearchService
from media_tag.storage.filesystem import FilesystemBackend, require_local_path
from media_tag.transforms.policy import review_state


class ExportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    database: str
    object_store: str
    workspace: str
    actor_id: str
    role: Literal["editor", "admin"] = "editor"
    asset_ids: tuple[str, ...] = PField(min_length=1, max_length=5000)
    release_id: str
    parent_release_id: str | None = None
    producer_revision: str
    template_dir: str | None = None
    review: dict | None = None


class SnapshotDB:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self, *, write=False):
        if write:
            raise ValueError("publication snapshot is read-only")
        yield self.connection


class CatalogView:
    """Reuse catalogue read/policy methods without calling its write constructor."""
    _authorize = staticmethod(Catalog._authorize)
    _snapshot = Catalog._snapshot


def build_bundle(settings, destination):
    settings = ExportSettings.model_validate(settings)
    principal = Principal(actor_id=settings.actor_id, role=settings.role)
    # Validate wire identifiers and revision before any output side effects.
    from pydantic import TypeAdapter
    from media_contracts.models import Id, Sha
    for value in (*settings.asset_ids, settings.release_id, *((settings.parent_release_id,) if settings.parent_release_id else ())):
        TypeAdapter(Id).validate_python(value)
    if re.fullmatch(r"[0-9a-f]{40}", settings.producer_revision) is None:
        raise ValueError("full source commit required")
    source = Path(settings.database)
    for path in (source, Path(settings.object_store), Path(settings.workspace), Path(destination)):
        if not path.is_absolute():
            raise ValueError("absolute publication paths required")
        require_local_path(path)
    destination = Path(destination).resolve()
    if destination.is_relative_to(Path(settings.object_store).resolve()) or destination.is_relative_to(Path(settings.workspace).resolve()):
        raise ValueError("bundle must not overlap source/workspace")
    destination.mkdir(parents=True, exist_ok=False)
    (destination / "README.md").write_text("# Portable publication bundle\n\nPrivate generated media and index; manifest.json is written only after a complete validated export. No credentials or original templates.\n", encoding="utf-8")
    blobs = destination / "objects"
    blobs.mkdir()
    (blobs / "README.md").write_text("Content-addressed selected publication bytes, including normalized evidence. Private; not Git.\n", encoding="utf-8")
    snapshot_path = destination / ".snapshot.db"
    output_refs = {}

    def save(read, expected=None):
        import tempfile
        import os
        fd, name = tempfile.mkstemp(dir=blobs)
        temporary = Path(name)
        try:
            actual, count = sha256(), 0
            with os.fdopen(fd, "wb") as output:
                while chunk := read(1024 * 1024):
                    count += len(chunk)
                    if count > 8 * 1024**3:
                        raise ValueError("publication object byte budget")
                    actual.update(chunk)
                    output.write(chunk)
            ref = Blob(sha256=actual.hexdigest(), size_bytes=count)
            if expected and ref != expected:
                raise ValueError("source changed during export")
            try:
                os.link(temporary, blobs / ref.sha256)
            except FileExistsError:
                pass
            output_refs[ref.sha256] = ref
            return ref
        finally:
            temporary.unlink(missing_ok=True)

    def save_json(value):
        from io import BytesIO
        return save(BytesIO(canonical(value)).read)

    readonly = snapshot = connection = None
    try:
        # Online backup observes one SQLite state, including committed WAL data.
        readonly = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        snapshot = sqlite3.connect(snapshot_path)
        readonly.backup(snapshot)
        snapshot.close()
        snapshot = None
        readonly.close()
        readonly = None
        connection = sqlite3.connect(snapshot_path.as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        database = SnapshotDB(connection)
        objects = FilesystemBackend(Path(settings.object_store))
        workspace = FilesystemBackend(Path(settings.workspace))
        catalog = CatalogView()
        catalog.database, catalog.object_store = database, objects
        catalog.backend_id, catalog.backends = "local", {"local": objects}
        if settings.template_dir:
            from media_tag.contracts.templates import load_template_bundle
            from media_tag.review.models import ReviewDeployment
            from media_tag.review.policy import ReviewPolicy
            bound = connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()[0]
            contract = load_template_bundle(Path(settings.template_dir), expected_fingerprint=bound)
            catalog.review_policy = ReviewPolicy(contract, ReviewDeployment.model_validate_json(canonical(settings.review or {})))
        elif settings.review:
            raise ValueError("review configuration requires the verified local template bundle")
        view = SimpleNamespace(database=database, catalog=catalog, workspace=Path(settings.workspace), files=workspace,
                               stages={stage: SimpleNamespace(require_artifact=True) for stage in ("ocr", "face", "clip", "visual_fields", "probe", "evidence")})
        view._artifacts = MethodType(Router._artifacts, view)
        files, drafts = FileService(view), DraftIndex(view)
        selected, todo = {}, list(settings.asset_ids)
        while todo:
            asset_id = todo.pop()
            if asset_id in selected:
                continue
            row = catalog._authorize(connection, asset_id, principal)
            if row["parent_asset_id"]:
                todo.append(row["parent_asset_id"])
            # A parent must also be readable by the publishing principal.
            files.authorize(asset_id, row["blob_sha256"], principal=principal, for_bytes=True)
            selected[asset_id] = row
        assets = []
        for asset_id, row in sorted(selected.items()):
            detail = files.details(asset_id, principal=principal)
            if not settings.template_dir and connection.execute("SELECT 1 FROM index_confirmations WHERE asset_id=?", (asset_id,)).fetchone():
                raise ValueError("index confirmation requires explicit template/review policy binding")
            opened = files.open(asset_id, row["blob_sha256"], principal=principal)
            try:
                blob = save(opened.read, Blob(sha256=row["blob_sha256"], size_bytes=detail["size_bytes"]))
                mime = opened.mime
            finally:
                opened.close()
            previews = []
            records, _ = files._previews(connection, asset_id, row["blob_sha256"])
            for record in records:
                preview = files.open_preview(asset_id, row["blob_sha256"], record["preview_id"], record["artifact"].sha256, principal=principal)
                try:
                    ref = save(preview.read, Blob(sha256=preview.digest, size_bytes=preview.size))
                    previews.append(Preview(preview_id=record["preview_id"], blob=ref, width=preview.dimensions[0], height=preview.dimensions[1], source_seconds=record["source_seconds"]))
                finally:
                    preview.close()
            # Use the same local medical-evidence eligibility rules as search.
            human = SearchService._document(SimpleNamespace(reviews=None), connection, row, None, persist=False)
            fields = [Field(field=f["field"], values=tuple(f["values"]), origin="human_effective", medical_confirmed=f["field"] in MEDICAL) for f in human["fields"]]
            projected = drafts.sync(connection, row, persist=False)
            if "unavailable" in projected["stages"].values():
                raise ValueError("current draft evidence unavailable; refuse incomplete publication")
            evidence = []
            by_task = {}
            for label in projected["labels"]:
                by_task.setdefault(label["task_id"], []).append(label)
            attempts = {TaskEnvelope.model_validate_json(r["envelope_json"]).task_id: r for r in files._attempts(connection, asset_id)}
            # A successful observation with no detected text/faces still has real
            # provenance. Include its report closure even when it emits no labels.
            seen_stages=set()
            for task_id,attempt in attempts.items():
                task=TaskEnvelope.model_validate_json(attempt['envelope_json'])
                if task.stage in seen_stages:continue
                seen_stages.add(task.stage)
                if task.stage in ('ocr','face','clip','visual_fields') and projected['stages'].get(task.stage) in ('ready','visual_features_ready_classification_missing'):
                    by_task.setdefault(task_id,[])
            for task_id, labels in by_task.items():
                attempt = attempts[task_id]
                task = TaskEnvelope.model_validate_json(attempt["envelope_json"])
                result = WorkerResult.model_validate_json(attempt["result_json"])
                report_ref, report = drafts._report(task, result)
                frame_refs = []
                for item in result.evidence:
                    if item.artifact.sha256 == blob.sha256:
                        frame_refs.append(blob)
                    else:
                        with workspace.open_read(item.artifact.ref) as stream:
                            ref = save(stream.read)
                        if ref.sha256 != item.artifact.sha256:
                            raise ValueError("evidence frame changed")
                        frame_refs.append(ref)
                texts = tuple(dict.fromkeys(label["text"] for label in labels))
                payload = EvidencePayload(asset_id=asset_id, source_sha256=blob.sha256, task_id=task_id, stage=task.stage,
                    producer_report_sha256=report_ref.sha256, producer_model_revision=report.get("model_revision"), labels=texts, frames=tuple(frame_refs))
                ref = save_json(payload)
                evidence_id = "ev-" + ref.sha256[:40]
                evidence.append(Evidence(evidence_id=evidence_id, blob=ref, source_sha256=blob.sha256))
                for label in labels:
                    fields.append(Field(field=label.get("field") or label["kind"], values=(label["text"],), origin="ai_draft", evidence_ids=(evidence_id,)))
            privacy, recipe, reviewed_hash, parent_sha = "not_applicable", None, None, None
            if row["kind"] == "derived":
                variant = connection.execute("SELECT * FROM variants WHERE asset_id=?", (asset_id,)).fetchone()
                privacy = review_state(connection, variant)
                recipe = variant["recipe_hash"]
                parent_sha = variant["source_sha256"]
                reviewed_hash = blob.sha256 if privacy == "approved" else None
            tech = detail["technical"]
            technical = Technical(kind=tech.get("kind") or "unknown", width=tech.get("display_width") or tech.get("width"),
                height=tech.get("display_height") or tech.get("height"), duration_seconds=tech.get("duration_seconds"),
                has_audio=tech.get("has_audio"), report_state=tech["report_state"])
            assets.append(Asset(asset_id=asset_id, owner_id=row["owner_id"], root_asset_id=row["root_asset_id"],
                parent_asset_id=row["parent_asset_id"], parent_sha256=parent_sha, revision=row["revision"], name=row["name"], kind=row["kind"],
                blob=blob, media_type=mime, technical=technical, fields=tuple(fields), sources=tuple(Source(**s) for s in detail["sources"]),
                previews=tuple(previews), evidence=tuple(evidence), index_status=detail["index_status"], privacy_state=privacy,
                recipe_sha256=recipe, review_output_sha256=reviewed_hash, review_recipe_sha256=recipe if privacy == "approved" else None))
        fingerprint = connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()[0]
        package = PublishedCatalog(template_fingerprint=fingerprint, assets=tuple(assets))
        catalog_ref = save_json(package)
        manifest = Manifest(release_id=settings.release_id, parent_release_id=settings.parent_release_id,
            producer_revision=settings.producer_revision, catalog=catalog_ref, objects=tuple(output_refs[k] for k in sorted(output_refs)))
        (destination / "manifest.json").write_bytes(canonical(manifest))
        return manifest
    finally:
        for handle in (connection, snapshot, readonly):
            if handle:
                handle.close()
        snapshot_path.unlink(missing_ok=True)
