from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import pytest
from media_tag.catalog.service import encode
from media_tag.maintenance.backup import snapshot,restore,verify_backup,RecoveryError
from search.test_public_api import run_cpu


def test_empty_catalogue_backup_and_restore_are_valid(env,tmp_path):
    result=snapshot(env.database.path,env.workspace,env.catalog.object_store.root,tmp_path/'empty-backup',offline_confirmed=True)
    assert result['files']==1
    receipt=restore(tmp_path/'empty-backup',tmp_path/'empty-restored',result['manifest_sha256'])
    assert receipt['files']==1 and (tmp_path/'empty-restored/objects').is_dir() and (tmp_path/'empty-restored/workspace').is_dir()


def source(sought,tmp_path):
    run_cpu(sought)
    return dict(database=sought.env.database.path,workspace=sought.env.workspace,object_store=sought.env.catalog.object_store.root,destination=tmp_path/'backup',offline_confirmed=True)


def test_full_wal_snapshot_and_fresh_restore_preserve_values_permissions_versions(sought,tmp_path):
    args=source(sought,tmp_path)
    with closing(sqlite3.connect(args['database'])) as keeper:
        keeper.execute('PRAGMA journal_mode=WAL')
        keeper.execute("INSERT INTO catalog_metadata VALUES('synthetic_wal_marker','retained')");keeper.commit()
        assert Path(str(args['database'])+'-wal').stat().st_size>0
        result=snapshot(**args)
    backend,manifest=verify_backup(args['destination'],result['manifest_sha256'])
    receipt=restore(args['destination'],tmp_path/'restored',result['manifest_sha256'])
    assert receipt['database_integrity']=='ok' and not receipt['configuration_restored'] and not receipt['service_started']
    with closing(sqlite3.connect(tmp_path/'restored/database.sqlite3')) as connection:
        assert connection.execute("SELECT value FROM catalog_metadata WHERE key='synthetic_wal_marker'").fetchone()[0]=='retained'
        assert connection.execute('SELECT asset_id,blob_sha256 FROM assets').fetchall()==[(sought.asset.asset_id,sought.asset.sha256)]
        assert connection.execute('SELECT count(*) FROM effective_values').fetchone()[0]>0
        assert connection.execute('SELECT subject_id FROM asset_permissions').fetchall()==[(sought.env.reader.actor_id,)]
        assert connection.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert any(m.path.startswith('workspace/jobs/text-index-') for m in manifest.files)
    assert any(m.path=='objects/objects/'+sought.asset.sha256[:2]+'/'+sought.asset.sha256 for m in manifest.files)
    assert not manifest.templates_included and not manifest.model_weights_included and not manifest.credentials_included
    assert sought.original.read_bytes()==Path(tmp_path/'restored/objects/objects'/sought.asset.sha256[:2]/sought.asset.sha256).read_bytes()


def test_operator_offline_assertion_and_fresh_destination_are_required(sought,tmp_path):
    args=source(sought,tmp_path)
    with pytest.raises(RecoveryError,match='stop_api'):snapshot(**(args|{'offline_confirmed':False}))
    assert not args['destination'].exists()
    result=snapshot(**args)
    with pytest.raises(RecoveryError,match='fresh_destination'):snapshot(**args)
    with pytest.raises(RecoveryError,match='fresh_destination'):restore(args['destination'],args['workspace'],result['manifest_sha256'])


@pytest.mark.parametrize('failure',['manifest','object','missing'])
def test_backup_tamper_or_missing_member_is_rejected_before_restore_destination(sought,tmp_path,failure):
    args=source(sought,tmp_path);result=snapshot(**args)
    if failure=='manifest':(args['destination']/'manifest.json').write_text('{}')
    else:
        target=next((args['destination']/'objects').rglob(sought.asset.sha256))
        if failure=='object':target.write_bytes(b'corrupt')
        else:target.unlink()
    with pytest.raises(Exception):restore(args['destination'],tmp_path/'restored',result['manifest_sha256'])
    assert not (tmp_path/'restored').exists()


def test_source_object_corruption_fails_without_complete_manifest(sought,tmp_path):
    args=source(sought,tmp_path)
    target=next(args['object_store'].rglob(sought.asset.sha256));target.write_bytes(b'corrupt')
    with pytest.raises(RecoveryError,match='source_object_integrity_failed'):snapshot(**args)
    assert not (args['destination']/'manifest.json').exists()


def test_workspace_secret_names_and_file_budget_are_explicitly_refused(sought,tmp_path):
    args=source(sought,tmp_path)
    with pytest.raises(RecoveryError,match='file_budget'):snapshot(**(args|{'max_files':1}))
    (args['workspace']/'jobs/credential.local.json').write_text('synthetic-private-credential')
    with pytest.raises(RecoveryError,match='credential_named'):snapshot(**args)
    assert not args['destination'].exists()


def test_source_or_destination_overlap_and_linked_path_are_blocked(sought,tmp_path):
    args=source(sought,tmp_path)
    with pytest.raises(RecoveryError,match='overlap'):snapshot(**(args|{'destination':args['workspace']/'backup'}))
    outside=tmp_path/'outside';outside.mkdir()
    link=args['workspace']/'jobs/linked'
    if os.name=='nt':
        result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(outside)],capture_output=True)
        assert result.returncode==0
    else:link.symlink_to(outside,target_is_directory=True)
    with pytest.raises(RecoveryError,match='linked'):snapshot(**args)
    assert not args['destination'].exists()


def test_db_migration_or_active_resource_state_is_refused(sought,tmp_path):
    args=source(sought,tmp_path)
    with sought.env.database.transaction(write=True) as connection:
        connection.execute("UPDATE router_tasks SET state='running' WHERE task_id=(SELECT task_id FROM router_tasks LIMIT 1)")
    with pytest.raises(RecoveryError,match='active_worker'):snapshot(**args)
    assert not args['destination'].exists()
    with sought.env.database.transaction(write=True) as connection:
        connection.execute("UPDATE router_tasks SET state='succeeded'")
        connection.execute("UPDATE schema_migrations SET checksum='corrupt' WHERE version=9")
    with pytest.raises(RecoveryError,match='migration_mismatch'):snapshot(**args)


def test_source_changes_during_copy_never_produce_complete_manifest(sought,tmp_path,monkeypatch):
    args=source(sought,tmp_path)
    from media_tag.maintenance import backup
    original=backup.copy_file;changed=False
    def mutate(*a,**k):
        nonlocal changed
        member=original(*a,**k)
        if member.path.startswith('workspace/') and not changed:
            changed=True;(args['workspace']/member.path.removeprefix('workspace/')).write_bytes(b'changed')
        return member
    monkeypatch.setattr(backup,'copy_file',mutate)
    with pytest.raises(RecoveryError,match='changed_during_backup'):snapshot(**args)
    assert not (args['destination']/'manifest.json').exists()


def test_registered_artifact_missing_or_changed_is_not_a_complete_backup(sought,tmp_path):
    args=source(sought,tmp_path)
    artifact=next(args['workspace'].glob('jobs/text-index-*/vectors.f32'))
    artifact.write_bytes(b'broken index')
    with pytest.raises(RecoveryError,match='registered_artifact'):snapshot(**args)
    assert not args['destination'].exists()


def test_cli_invalid_secret_config_is_bounded_and_does_not_start_service(tmp_path):
    path=tmp_path/'invalid.local.json';path.write_text('{"credential":"never-print-this"}')
    result=subprocess.run([sys.executable,'-m','media_tag','backup-offline','--config',str(path),'--destination',str(tmp_path/'backup'),'--offline-confirmed'],capture_output=True,timeout=10)
    assert result.returncode==2 and result.stdout==b'' and b'never-print-this' not in result.stderr
    assert json.loads(result.stderr)=={'error':'offline_maintenance_failed'} and not (tmp_path/'backup').exists()


@pytest.mark.parametrize('version', [9, 10])
def test_previous_backup_still_restores_and_can_migrate(env,tmp_path,version):
    from media_tag.catalog import Database
    with env.database.transaction(write=True) as c:
        c.execute('DROP TABLE analysis_intents')
        if version == 9:c.execute('DROP TABLE ai_draft_documents')
        c.execute('DELETE FROM schema_migrations WHERE version>?', (version,))
        c.execute(f'PRAGMA user_version={version}')
    target=tmp_path/f'v{version}-backup'
    result=snapshot(env.database.path,env.workspace,env.catalog.object_store.root,target,offline_confirmed=True)
    restore(target,tmp_path/f'v{version}-restored',result['manifest_sha256'])
    database=Database(tmp_path/f'v{version}-restored/database.sqlite3');database.initialize()
    with database.transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==11
        assert c.execute('SELECT count(*) FROM analysis_intents').fetchone()[0]==0
