from hashlib import sha256
import json
import subprocess
import sys
import threading
import time

import psutil
import pytest

from media_tag.catalog import Database,CatalogError
import media_tag.catalog.database as module
from media_tag.contracts.models import WorkerResult
from media_tag.models.contracts import ModelFailure
from media_tag.transforms.models import ScanOptions,ScanSubmission
from media_tag.transforms.processes import ProcessTree
from media_tag.transforms.runtime import RedactionHandlers


@pytest.mark.parametrize('options,code',[(dict(max_frames=2),'frame_budget'),(dict(max_seconds=.5),'duration_budget'),
    (dict(max_pixels=1000),'pixel_budget'),(dict(max_total_pixels=1000),'pixel_budget')])
def test_actual_budget_failures_never_become_successful_partial_scans(redaction,options,code):
    env=redaction
    job=env.redactions.start_scan(ScanSubmission(request_id='budget',parent_asset_id=env.parent.asset_id,expected_revision=1,
        options=ScanOptions.model_validate_json(json.dumps(options))),principal=env.owner)
    assert env.executor.run_once()['accepted']
    status=env.router.get_job(job['job_id'],principal=env.owner)
    assert status['state']=='needs_attention' and not status['resources']
    with env.database.transaction() as connection:
        result=WorkerResult.model_validate_json(connection.execute('SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?',(job['job_id'],)).fetchone()[0])
    assert result.status=='failed' and result.errors[0].code==code and not result.artifacts


@pytest.mark.parametrize('query_failure',[False,True])
def test_real_windows_job_kills_and_joins_grandchildren(tmp_path,monkeypatch,query_failure):
    marker=tmp_path/'child.pid'; gate=tmp_path/'start.ready'
    child="import time; time.sleep(30)"
    parent="import pathlib,subprocess,sys,time; p=pathlib.Path(sys.argv[1]); exec('while not p.exists(): time.sleep(.01)'); c=subprocess.Popen([sys.executable,'-c',sys.argv[3]]); pathlib.Path(sys.argv[2]).write_text(str(c.pid)); time.sleep(30)"
    tree=ProcessTree(); process=subprocess.Popen([sys.executable,'-c',parent,str(gate),str(marker),child],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    member=None
    try:
        tree.assign(process); gate.write_text('ready')
        deadline=time.monotonic()+5
        while not marker.exists() and time.monotonic()<deadline: time.sleep(.02)
        assert marker.exists(); member=psutil.Process(int(marker.read_text())); assert member.is_running()
        if query_failure:
            original=tree.kernel.QueryInformationJobObject; calls=[0]
            def fail_once(*args):
                calls[0]+=1
                return False if calls[0]==1 else original(*args)
            monkeypatch.setattr(tree.kernel,'QueryInformationJobObject',fail_once)
        tree.stop_and_close(); process.wait(timeout=5)
        if query_failure: assert calls[0]>=2
        member.wait(timeout=5)  # A non-child which already disappeared can return no exit code on Windows.
    finally:
        if process.poll() is None: process.kill(); process.wait(timeout=5)
        if member and member.is_running(): member.kill(); member.wait(timeout=5)


@pytest.mark.parametrize('fault',['cancelled','redaction_timeout'])
def test_handler_stops_real_process_before_returning(redaction,monkeypatch,fault):
    env=redaction
    env.redactions.start_scan(ScanSubmission(request_id='stop',parent_asset_id=env.parent.asset_id,expected_revision=1),principal=env.owner)
    lease=env.router.claim(principal=env.workers[0]); stopped=threading.Event(); original=subprocess.Popen; processes=[]
    def slow(*args,**kwargs):
        process=original([sys.executable,'-c','import time; time.sleep(30)'],**kwargs); processes.append(process)
        if fault=='cancelled': stopped.set()
        return process
    monkeypatch.setattr('media_tag.transforms.runtime.subprocess.Popen',slow)
    from media_tag.contracts.models import TaskLimits
    lease=lease.model_copy(update={'task':lease.task.model_copy(update={'limits':TaskLimits(timeout_seconds=1)})})
    with pytest.raises(ModelFailure,match=fault): RedactionHandlers(env.redactions)(lease,stopped)
    assert len(processes)==1 and processes[0].poll() is not None


def test_v3_migration_failure_rolls_back_and_preserves_existing_data(tmp_path,monkeypatch):
    import sqlite3
    path=tmp_path/'synthetic-v3.sqlite3'; connection=sqlite3.connect(path)
    for version,statements in [(1,module.SCHEMA),(2,module.ROUTER_SCHEMA),(3,module.UPLOAD_SCHEMA)]:
        for statement in statements: connection.execute(statement)
        connection.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(version,sha256('\n'.join(statements).encode()).hexdigest(),'synthetic'))
    connection.execute("INSERT INTO catalog_metadata VALUES('synthetic','keep')"); connection.execute('PRAGMA user_version=3');connection.commit();connection.close()
    schema=module.REDACTION_SCHEMA; monkeypatch.setattr(module,'REDACTION_SCHEMA',(*schema,'invalid synthetic SQL'))
    with pytest.raises(sqlite3.OperationalError): Database(path).initialize()
    with Database(path).transaction() as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==3
        assert not connection.execute("SELECT 1 FROM sqlite_master WHERE name='privacy_reviews'").fetchone()
        assert connection.execute('SELECT value FROM catalog_metadata').fetchone()[0]=='keep'
    monkeypatch.setattr(module,'REDACTION_SCHEMA',schema); Database(path).initialize()
    with Database(path).transaction(write=True) as connection:
        assert connection.execute('PRAGMA user_version').fetchone()[0]==12
        connection.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=4")
    with pytest.raises(CatalogError,match='checksum'): Database(path).initialize()


def test_anamorphic_source_is_rejected_instead_of_silently_stretched(redaction,tools):
    from media_tag.storage.filesystem import FilesystemBackend
    env=redaction; path=env.originals/'synthetic-sar.mp4'
    tools.ffmpeg_run(['-v','error','-i',str(env.parent_path),'-vf','setsar=2','-c:v','libx264',str(path)],timeout=10)
    asset=env.catalog.register_file(FilesystemBackend(env.originals),path.name,source_backend_id='synthetic-sar',principal=env.owner,
        request_id='sar',name='Synthetic anamorphic source')
    job=env.redactions.start_scan(ScanSubmission(request_id='sar',parent_asset_id=asset.asset_id,expected_revision=1),principal=env.owner)
    assert env.executor.run_once()['accepted']
    with env.database.transaction() as connection:
        result=WorkerResult.model_validate_json(connection.execute('SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?',(job['job_id'],)).fetchone()[0])
    assert result.status=='failed' and result.errors[0].code=='unsupported_pixel_aspect_ratio'
