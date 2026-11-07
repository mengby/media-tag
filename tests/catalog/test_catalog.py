from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3

import pytest
from pydantic import ValidationError

from media_tag.catalog import AccessDenied, Catalog, CatalogError, LegacyAlias, NotFound, Principal, RevisionConflict, SourceInput, UnsupportedRevision
from media_tag.contracts.models import EffectiveValue, FieldValue, TaskEnvelope, WorkerResult
from media_tag.storage import BackendNotConfigured, IntegrityError, StorageError, UnsafePath
from media_tag.storage.oss import OssBackend
from media_tag.storage.filesystem import FilesystemBackend


def register(env, *, key="synthetic.bin", data=b"synthetic bytes", request="synthetic-request", actor=None, **kwargs):
    (env.originals / key).parent.mkdir(parents=True, exist_ok=True)
    (env.originals / key).write_bytes(data)
    return env.catalog.register_file(env.source, key, source_backend_id="synthetic-originals", principal=actor or env.actor, request_id=request, name="synthetic name", **kwargs)


def test_same_name_different_bytes_and_same_bytes_different_sources(setup_catalog):
    env = setup_catalog
    first = register(env, key="one/same.bin", source=SourceInput(label="synthetic-source-one"))
    different = register(env, key="two/same.bin", data=b"different synthetic bytes", request="second")
    duplicate = register(env, key="three/same.bin", request="third", source=SourceInput(label="synthetic-source-two"))
    assert len({first.asset_id, different.asset_id, duplicate.asset_id}) == 3
    assert first.sha256 != different.sha256 and first.sha256 == duplicate.sha256
    assert first.sources[0].label != duplicate.sources[0].label
    assert first.sources[0].license_status == duplicate.sources[0].license_status == "unverified"
    assert first.index_status == duplicate.index_status == "ai_draft"
    assert first.root_asset_id == first.asset_id and duplicate.root_asset_id == duplicate.asset_id
    with env.database.transaction() as connection:
        assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM asset_sources").fetchone()[0] == 3
        assert connection.execute("SELECT count(*) FROM storage_locations").fetchone()[0] == 2
    assert (env.originals / "one/same.bin").read_bytes() == b"synthetic bytes"


def test_request_retry_alias_and_conflicts_are_atomic(setup_catalog):
    env = setup_catalog
    alias = LegacyAlias(namespace="synthetic-legacy", alias="synthetic-old-id")
    first = register(env, aliases=(alias,))
    repeat = env.catalog.register_file(env.source, "synthetic.bin", source_backend_id="synthetic-originals", principal=env.actor, request_id="synthetic-request", name="synthetic name", aliases=(alias,))
    assert repeat.asset_id == first.asset_id
    assert env.catalog.resolve_alias(alias.namespace, alias.alias, principal=env.actor).asset_id == first.asset_id
    with pytest.raises(RevisionConflict):
        register(env, data=b"changed", aliases=(alias,))
    with pytest.raises(RevisionConflict):
        register(env, request="new-request", aliases=(alias,))
    assert len(env.catalog.snapshot(principal=env.actor)) == 1
    assert len(env.catalog.history(first.asset_id, principal=env.actor)) == 1


def test_dedup_never_grants_another_account_access(setup_catalog):
    env = setup_catalog
    first = register(env, source=SourceInput(label="synthetic-private-one"))
    other = Principal(actor_id="synthetic-other")
    assert env.catalog.snapshot(principal=other) == ()
    with pytest.raises(AccessDenied):
        env.catalog.get_asset(first.asset_id, principal=other)
    with pytest.raises(AccessDenied):
        with env.catalog.open_content(first.asset_id, principal=other):
            pytest.fail("unauthorized file opened")
    second = register(env, request="other-request", actor=other, source=SourceInput(label="synthetic-private-two"))
    assert second.sha256 == first.sha256 and second.asset_id != first.asset_id
    visible = env.catalog.snapshot(principal=other)
    assert len(visible) == 1 and visible[0].sources[0].label == "synthetic-private-two"


def test_grant_revoke_revision_and_owner_control(setup_catalog):
    env = setup_catalog
    item = register(env)
    other = Principal(actor_id="synthetic-other")
    granted = env.catalog.change_access(item.asset_id, principal=env.actor, expected_revision=1,
                                        subject_id=other.actor_id, permission="edit", reason="synthetic grant")
    assert granted.revision == 2
    edited = env.catalog.revise(item.asset_id, principal=other, expected_revision=2, name="synthetic renamed", reason="synthetic edit")
    assert edited.revision == 3
    with pytest.raises(AccessDenied):
        env.catalog.change_access(item.asset_id, principal=other, expected_revision=3,
                                  subject_id="synthetic-third", permission="read", reason="forbidden")
    env.catalog.change_access(item.asset_id, principal=env.actor, expected_revision=3, subject_id=other.actor_id,
                              permission="edit", reason="synthetic revoke", revoke=True)
    assert env.catalog.snapshot(principal=other) == ()
    with pytest.raises(AccessDenied):
        env.catalog.history(item.asset_id, principal=other)


def test_database_failure_rolls_back_every_record_and_retry_reuses_orphan(setup_catalog):
    env = setup_catalog
    (env.originals / "synthetic.bin").write_bytes(b"synthetic original")
    with env.database.transaction(write=True) as connection:
        connection.execute("CREATE TRIGGER synthetic_fail BEFORE INSERT ON asset_sources BEGIN SELECT RAISE(ABORT,'synthetic failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        env.catalog.register_file(env.source, "synthetic.bin", source_backend_id="synthetic-originals", principal=env.actor, request_id="synthetic", name="synthetic")
    with env.database.transaction(write=True) as connection:
        for table in ("blobs", "assets", "storage_locations", "asset_sources", "registrations", "field_revisions"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        connection.execute("DROP TRIGGER synthetic_fail")
    assert env.catalog.snapshot(principal=env.actor) == ()
    assert (env.originals / "synthetic.bin").read_bytes() == b"synthetic original"
    orphan_files = list(env.objects.glob("objects/*/*"))
    assert len(orphan_files) == 1
    item = env.catalog.register_file(env.source, "synthetic.bin", source_backend_id="synthetic-originals", principal=env.actor, request_id="synthetic", name="synthetic")
    assert len(list(env.objects.glob("objects/*/*"))) == 1
    assert item.size_bytes == len(b"synthetic original")


def test_failed_acl_audit_rolls_back_permissions_and_revision(setup_catalog):
    env = setup_catalog
    item = register(env)
    with pytest.raises(ValidationError):
        env.catalog.change_access(item.asset_id, principal=env.actor, expected_revision=1, subject_id="synthetic-other",
                                  permission="read", reason="")
    assert env.catalog.get_asset(item.asset_id, principal=env.actor).revision == 1
    assert env.catalog.snapshot(principal=Principal(actor_id="synthetic-other")) == ()


def value(env, revision, *, field="synthetic_ai_text", actor=None):
    return EffectiveValue(field=field, value=FieldValue(state="observed", values=("synthetic human value",)),
                          actor_id=actor or env.actor.actor_id, revision=revision, evidence_ids=("synthetic-e",),
                          confirmed_at=datetime.now(timezone.utc))


def test_human_revision_actor_evidence_and_multi_field_rollback(setup_catalog):
    env = setup_catalog
    item = register(env)
    revised = env.catalog.revise(item.asset_id, principal=env.actor, expected_revision=1, reason="synthetic review", values=(value(env, 2),))
    assert revised.effective_values[0].value.values == ("synthetic human value",)
    assert revised.revision == 2
    for forbidden in ("synthetic_script", "synthetic_select", "synthetic_medical", "synthetic_unknown"):
        with pytest.raises(UnsupportedRevision):
            env.catalog.revise(item.asset_id, principal=env.actor, expected_revision=2, reason="synthetic blocked",
                                values=(value(env, 3, field="synthetic_note"), value(env, 3, field=forbidden)))
        assert len(env.catalog.get_asset(item.asset_id, principal=env.actor).effective_values) == 1
    with pytest.raises(RevisionConflict):
        env.catalog.revise(item.asset_id, principal=env.actor, expected_revision=2, reason="synthetic blocked",
                            values=(value(env, 3, actor="synthetic-impostor"),))
    history = env.catalog.history(item.asset_id, principal=env.actor)
    assert len(history) == 2 and history[-1]["actor_id"] == env.actor.actor_id
    assert json.loads(history[-1]["after_json"])["values"][0]["evidence_ids"] == ["synthetic-e"]


def task_and_result(item, *, field="synthetic_ai_text"):
    task_id = f"synthetic-task-{field}"
    task = TaskEnvelope.model_validate_json(json.dumps({
        "task_id": task_id, "job_id": "synthetic-job", "asset_id": item.asset_id,
        "asset_revision": item.revision, "stage": "synthetic-tag", "attempt": 1, "lease_epoch": 1,
        "input_fingerprint": item.sha256,
        "input_refs": [{"kind": "synthetic", "ref": "inputs/synthetic.bin", "sha256": item.sha256}],
        "versions": {"rules_version": "synthetic-v1", "preprocess_version": "synthetic-v1"},
        "target_fields": [field], "output_prefix": f"jobs/synthetic-job/runs/{task_id}/attempt-1/"}))
    result = WorkerResult.model_validate_json(json.dumps({
        "task_id": task.task_id, "attempt": task.attempt, "lease_epoch": task.lease_epoch,
        "input_fingerprint": task.input_fingerprint, "versions": task.versions.model_dump(),
        "status": "succeeded", "elapsed_seconds": 0.1,
        "evidence": [{"evidence_id": "synthetic-e", "kind": "image", "source_sha256": item.sha256,
                      "artifact": task.input_refs[0].model_dump()}],
        "suggestions": [{"suggestion_id": "synthetic-s", "field": field, "value": {"state": "observed", "values": ["synthetic AI value"]},
                         "origin": "ai", "producer_version": "synthetic-v1", "evidence_ids": ["synthetic-e"]}]}))
    return task, result


def test_worker_suggestions_never_overwrite_human_values_and_do_not_grant_read(setup_catalog):
    env = setup_catalog
    item = register(env)
    worker = Principal(actor_id="synthetic-worker", role="worker")
    item = env.catalog.change_access(item.asset_id, principal=env.actor, expected_revision=1, subject_id=worker.actor_id,
                                     permission="suggest", reason="synthetic task grant")
    item = env.catalog.revise(item.asset_id, principal=env.actor, expected_revision=2, reason="synthetic human review", values=(value(env, 3),))
    task, result = task_and_result(item)
    assert env.catalog.record_suggestions(task, result, principal=worker) is None
    env.catalog.record_suggestions(task, result, principal=worker)
    with env.database.transaction() as connection:
        runs = connection.execute("SELECT task_json,result_json FROM suggestion_runs").fetchall()
        assert len(runs) == 1
        assert TaskEnvelope.model_validate_json(runs[0][0]) == task
        assert WorkerResult.model_validate_json(runs[0][1]) == result
    after = env.catalog.get_asset(item.asset_id, principal=env.actor)
    assert after.revision == 3 and len(after.suggestions) == 1
    assert after.effective_values[0].value.values == ("synthetic human value",)
    assert after.suggestions[0].value.values == ("synthetic AI value",)
    assert env.catalog.snapshot(principal=worker) == ()
    with pytest.raises(AccessDenied):
        env.catalog.get_asset(item.asset_id, principal=worker)
    with pytest.raises(AccessDenied):
        env.catalog.revise(item.asset_id, principal=worker, expected_revision=3, reason="forbidden", name="forbidden")
    item = env.catalog.revise(item.asset_id, principal=env.actor, expected_revision=3, reason="synthetic newer edit", name="synthetic newer name")
    with pytest.raises(RevisionConflict):
        env.catalog.record_suggestions(task, result, principal=worker)
    fresh_task, fresh_result = task_and_result(item, field="synthetic_note")
    with pytest.raises(UnsupportedRevision):
        env.catalog.record_suggestions(fresh_task, fresh_result, principal=worker)


def test_two_concurrent_revisions_have_one_winner(setup_catalog):
    env = setup_catalog
    item = register(env)
    def edit(number):
        try:
            return env.catalog.revise(item.asset_id, principal=env.actor, expected_revision=1, reason="synthetic concurrent edit", name=f"synthetic {number}").revision
        except RevisionConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(edit, (1, 2)))
    assert sorted(map(str, outcomes)) == ["2", "conflict"]
    assert len(env.catalog.history(item.asset_id, principal=env.actor)) == 2


def test_concurrent_identical_registration_is_idempotent(setup_catalog):
    env = setup_catalog
    (env.originals / "synthetic.bin").write_bytes(b"synthetic concurrent content")
    def ingest(_):
        return env.catalog.register_file(env.source, "synthetic.bin", source_backend_id="synthetic-originals", principal=env.actor, request_id="synthetic-same", name="synthetic").asset_id
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(ingest, (1, 2)))
    assert len(set(outcomes)) == 1
    assert len(env.catalog.snapshot(principal=env.actor)) == 1


def test_snapshot_is_detached_and_sql_read_transaction_is_consistent(setup_catalog):
    env = setup_catalog
    first = register(env)
    frozen = env.catalog.snapshot(principal=env.actor)
    with env.database.transaction() as reader:
        assert reader.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
        register(env, key="second.bin", request="synthetic-second")
        assert reader.execute("SELECT count(*) FROM assets").fetchone()[0] == 1
    env.catalog.revise(first.asset_id, principal=env.actor, expected_revision=1, name="synthetic new", reason="synthetic update")
    assert len(frozen) == 1 and frozen[0].name == "synthetic name"
    assert len(env.catalog.snapshot(principal=env.actor)) == 2


def test_missing_or_corrupt_managed_copy_never_falls_back_to_original(setup_catalog):
    env = setup_catalog
    item = register(env)
    with env.catalog.open_content(item.asset_id, principal=env.actor) as stream:
        assert stream.read() == b"synthetic bytes"
    object_path = next(env.objects.glob("objects/*/*"))
    object_path.write_bytes(b"corrupt")
    with pytest.raises(IntegrityError):
        with env.catalog.open_content(item.asset_id, principal=env.actor):
            pytest.fail("corrupt file returned")
    object_path.unlink()
    with pytest.raises(StorageError):
        with env.catalog.open_content(item.asset_id, principal=env.actor):
            pytest.fail("original fallback returned")
    assert (env.originals / "synthetic.bin").read_bytes() == b"synthetic bytes"


def test_archive_only_hides_record_and_identity_is_sql_immutable(setup_catalog):
    env = setup_catalog
    item = register(env)
    with pytest.raises(sqlite3.IntegrityError):
        with env.database.transaction(write=True) as connection:
            connection.execute("UPDATE assets SET blob_sha256=? WHERE asset_id=?", ("b" * 64, item.asset_id))
    env.catalog.archive(item.asset_id, principal=env.actor, expected_revision=1, reason="synthetic archive")
    assert env.catalog.snapshot(principal=env.actor) == ()
    with pytest.raises(NotFound):
        env.catalog.get_asset(item.asset_id, principal=env.actor)
    assert (env.originals / "synthetic.bin").exists()
    assert len(list(env.objects.glob("objects/*/*"))) == 1


def test_catalogue_requires_known_contract_and_separate_source_root(setup_catalog):
    env = setup_catalog
    with pytest.raises(CatalogError):
        Catalog(env.database, env.store, contract=replace(env.contract, baseline_verified=False))
    with pytest.raises(CatalogError):
        Catalog(env.database, env.store, contract=replace(env.contract, fingerprint="f" * 64))
    with pytest.raises(UnsafePath):
        env.catalog.register_file(env.store, "synthetic.bin", source_backend_id="synthetic-originals", principal=env.actor, request_id="synthetic", name="synthetic")


def test_original_changed_by_external_writer_is_not_registered(setup_catalog, monkeypatch):
    env = setup_catalog
    source_path = env.originals / "synthetic.bin"
    source_path.write_bytes(b"synthetic original")
    ingest = env.store.ingest
    def concurrent_change(stream):
        blob = ingest(stream)
        source_path.write_bytes(b"synthetic changed by another writer")
        return blob
    monkeypatch.setattr(env.store, "ingest", concurrent_change)
    with pytest.raises(IntegrityError, match="source changed"):
        env.catalog.register_file(env.source, "synthetic.bin", source_backend_id="synthetic-originals", principal=env.actor, request_id="synthetic", name="synthetic")
    assert env.catalog.snapshot(principal=env.actor) == ()


def test_worker_permission_wrong_source_and_changed_suggestion_id_are_rejected(setup_catalog):
    env = setup_catalog
    item = register(env)
    worker = Principal(actor_id="synthetic-worker", role="worker")
    task, result = task_and_result(item)
    with pytest.raises(AccessDenied):
        env.catalog.record_suggestions(task, result, principal=worker)
    item = env.catalog.change_access(item.asset_id, principal=env.actor, expected_revision=1, subject_id=worker.actor_id,
                                     permission="suggest", reason="synthetic task grant")
    task, result = task_and_result(item)
    bad_data = json.loads(result.model_dump_json())
    bad_data["evidence"][0]["source_sha256"] = "a" * 64
    with pytest.raises(RevisionConflict, match="file version"):
        env.catalog.record_suggestions(task, WorkerResult.model_validate_json(json.dumps(bad_data)), principal=worker)
    env.catalog.record_suggestions(task, result, principal=worker)
    bad_data = json.loads(result.model_dump_json())
    bad_data["suggestions"][0]["value"]["values"] = ["synthetic changed"]
    with pytest.raises(RevisionConflict):
        env.catalog.record_suggestions(task, WorkerResult.model_validate_json(json.dumps(bad_data)), principal=worker)
    medical_task, medical_result = task_and_result(item, field="synthetic_medical")
    with pytest.raises(UnsupportedRevision):
        env.catalog.record_suggestions(medical_task, medical_result, principal=worker)


def test_source_license_cannot_be_auto_confirmed():
    with pytest.raises(ValidationError):
        SourceInput(license_status="verified")


def test_oss_mapping_failure_is_explicit(setup_catalog):
    env = setup_catalog
    item = register(env)
    with env.database.transaction(write=True) as connection:
        connection.execute("UPDATE storage_locations SET backend_id='oss' WHERE blob_sha256=?", (item.sha256,))
    catalog = Catalog(env.database, env.store, contract=env.contract, read_backends={"oss": OssBackend()})
    with pytest.raises(BackendNotConfigured):
        with catalog.open_content(item.asset_id, principal=env.actor):
            pytest.fail("unconfigured OSS returned a fallback")


def test_physical_backend_root_can_change_without_changing_identity(setup_catalog, tmp_path):
    env = setup_catalog
    item = register(env)
    new_root = tmp_path / "synthetic-new-object-root"
    new_root.mkdir()
    new_store = FilesystemBackend(new_root, writable=True)
    with env.catalog.open_content(item.asset_id, principal=env.actor) as stream:
        new_store.ingest(stream)
    reopened = Catalog(env.database, new_store, contract=env.contract)
    assert reopened.get_asset(item.asset_id, principal=env.actor) == item
    with reopened.open_content(item.asset_id, principal=env.actor) as stream:
        assert stream.read() == b"synthetic bytes"


def test_legacy_aliases_are_scoped_and_visible_ambiguity_is_explicit(setup_catalog):
    env = setup_catalog
    alias = LegacyAlias(namespace="synthetic-legacy", alias="synthetic-same")
    first = register(env, aliases=(alias,))
    other = Principal(actor_id="synthetic-other")
    second = register(env, actor=other, request="other-request", aliases=(alias,))
    assert env.catalog.resolve_alias(alias.namespace, alias.alias, principal=env.actor).asset_id == first.asset_id
    env.catalog.change_access(second.asset_id, principal=other, expected_revision=1, subject_id=env.actor.actor_id,
                              permission="read", reason="synthetic shared")
    with pytest.raises(RevisionConflict, match="multiple"):
        env.catalog.resolve_alias(alias.namespace, alias.alias, principal=env.actor)


@pytest.mark.parametrize("reference", ["https://user:password@example.test/file", "https://example.test/file?X-Amz-Signature=secret", "https://example.test/file?token=secret", "file:///secret"])
def test_source_references_cannot_persist_credentials_or_temporary_links(reference):
    with pytest.raises(ValidationError):
        SourceInput(reference=reference)
