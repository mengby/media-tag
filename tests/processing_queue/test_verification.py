from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from media_tag.catalog import AccessDenied, Principal
from media_tag.cleaning import CleaningRecipe
from media_tag.models.contracts import ModelFailure
from media_tag.processing_queue import verification
from media_tag.workers.evidence import _directory

from .conftest import register
from .test_queue import submission,run


def refs(queue,job):
    with queue.env.database.transaction() as connection:
        raw=connection.execute('SELECT a.result_json FROM router_attempts a JOIN router_tasks t ON t.task_id=a.task_id WHERE t.job_id=? AND a.lease_epoch=t.lease_epoch',(job,)).fetchone()[0]
    return {item['kind']:item for item in json.loads(raw)['artifacts']}


@pytest.mark.parametrize('kind',['processing_output','processing_report','processing_core_report'])
def test_oversize_actual_file_rejected_before_any_read(queue,monkeypatch,kind):
    from media_tag.cleaning.models import CleaningLimits
    options=queue.options.model_copy(update={'cleaning':CleaningLimits(max_output_bytes=4096)})
    queue.service.options=options;queue.handler.options=options
    asset=register(queue)
    job,status=run(queue,submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')))
    assert status['processing_completed']
    ref=refs(queue,job)[kind]['ref']
    limit=4096 if kind=='processing_output' else verification.REPORT_LIMIT
    with (queue.env.workspace/ref).open('r+b') as stream:stream.truncate(limit+1)
    original=queue.env.router.files.open_read
    reads=[]
    class Guard:
        def __init__(self,stream):self.stream=stream
        def fileno(self):return self.stream.fileno()
        def read(self,*args):
            reads.append(ref)
            raise AssertionError('oversize artifact must be rejected using fstat before read')
    @contextmanager
    def checked(key):
        with original(key) as stream:
            yield Guard(stream) if key==ref else stream
    monkeypatch.setattr(queue.env.router.files,'open_read',checked)
    monkeypatch.setattr(queue.env.router,'_artifacts',lambda *args:pytest.fail('generic unbounded artifact scan forbidden'))
    after=queue.service.status(job,principal=queue.env.owner)
    assert after['state']=='processing_evidence_unavailable' and not reads


def test_actual_output_size_must_match_report_before_hashing_media(queue,monkeypatch):
    asset=register(queue)
    job,status=run(queue,submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')))
    ref=refs(queue,job)['processing_output']['ref']
    with (queue.env.workspace/ref).open('ab') as stream:stream.write(b'x')
    original=verification._read
    def checked(router,artifact,*args,**kwargs):
        assert artifact.kind!='processing_output','size mismatch must be detected before output hash pass'
        return original(router,artifact,*args,**kwargs)
    monkeypatch.setattr(verification,'_read',checked)
    after=queue.service.status(job,principal=queue.env.owner)
    assert after['state']=='processing_evidence_unavailable'


@pytest.mark.parametrize('change',['cancel','revoke','revision','epoch','unreleased'])
def test_status_rechecks_authority_and_current_attempt_after_long_hash(queue,monkeypatch,change):
    asset=register(queue);actor=Principal(actor_id='hash-reader')
    updated=queue.env.catalog.change_access(asset.asset_id,principal=queue.env.owner,expected_revision=asset.revision,
        subject_id=actor.actor_id,permission='edit',reason='test authorization')
    spec=submission(queue,updated,CleaningRecipe(source_sha256=asset.sha256,content_kind='image'))
    job=queue.service.request(spec,principal=actor)['job_id'];queue.executor.run_once()
    assert queue.service.status(job,principal=actor)['processing_completed']
    original=verification._read;changed=[]
    def during(router,artifact,*args,**kwargs):
        value=original(router,artifact,*args,**kwargs)
        if artifact.kind=='processing_output' and not changed:
            changed.append(True)
            if change=='cancel':queue.env.router.cancel(job,principal=actor)
            elif change=='revoke':queue.env.catalog.change_access(asset.asset_id,principal=queue.env.owner,expected_revision=updated.revision,
                subject_id=actor.actor_id,permission='edit',reason='revoke during hash',revoke=True)
            elif change=='revision':queue.env.catalog.revise(asset.asset_id,principal=queue.env.owner,expected_revision=updated.revision,reason='change during hash',name='changed')
            else:
                with queue.env.database.transaction(write=True) as connection:
                    if change=='epoch':connection.execute('UPDATE router_tasks SET lease_epoch=lease_epoch+1 WHERE job_id=?',(job,))
                    else:connection.execute('UPDATE router_attempts SET released_at=NULL WHERE task_id IN (SELECT task_id FROM router_tasks WHERE job_id=?)',(job,))
        return value
    monkeypatch.setattr(verification,'_read',during)
    if change=='revoke':
        with pytest.raises(AccessDenied):queue.service.status(job,principal=actor)
    else:
        after=queue.service.status(job,principal=actor)
        assert not after['processing_completed'] and 'output_sha256' not in after
        assert after['state']==('cancelled' if change=='cancel' else 'source_changed' if change=='revision' else 'processing_evidence_unavailable')
    assert changed


@pytest.mark.parametrize('behavior',['fast_log','environment'])
def test_real_gated_child_final_log_budget_and_restricted_environment(queue,monkeypatch,behavior):
    import media_tag.processing_queue.runtime as runtime
    original=subprocess.Popen
    folder=_directory(queue.env.workspace,'native-'+behavior)
    secret_names=('PROCESSING_TEST_API_TOKEN','OSS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY')
    for name in secret_names:monkeypatch.setenv(name,'synthetic-do-not-inherit')
    captured=[]
    script = '''import json,os,sys,time
from pathlib import Path
request=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
gate=Path(request['start_gate'])
while not gate.is_file():time.sleep(.01)
if sys.argv[3]=='fast_log':
 sys.stdout.buffer.write(b'x'*(2*1024**2+1));sys.stdout.buffer.flush()
else:
 Path(sys.argv[2]).write_text(json.dumps({'state':'failed','issue_code':'synthetic','inherited':[name for name in ('PROCESSING_TEST_API_TOKEN','OSS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY') if name in os.environ]}),encoding='utf8')
'''
    class JoinedOnPoll:
        def __init__(self,process):self.process=process
        def __getattr__(self,name):return getattr(self.process,name)
        def poll(self):return self.process.wait(timeout=5)
    def child(command,**kwargs):
        captured.append(kwargs['env'])
        process=original([sys.executable,'-c',script,command[-2],command[-1],behavior],**kwargs)
        return JoinedOnPoll(process) if behavior=='fast_log' else process
    monkeypatch.setattr(runtime.subprocess,'Popen',child)
    request={'start_gate':str(folder/'start.ready')}
    if behavior=='fast_log':
        with pytest.raises(ModelFailure) as error:
            queue.handler.invoke(request,folder,time.monotonic()+10,threading.Event())
        assert error.value.code=='processing_log_budget'
        assert (folder/'native.stdout.log').stat().st_size==2*1024**2+1
    else:
        response=queue.handler.invoke(request,folder,time.monotonic()+10,threading.Event())
        assert response['inherited']==[]
    assert captured and not set(secret_names)&set(captured[0])
    assert captured[0]['OMP_NUM_THREADS']=='1' and captured[0]['HF_HUB_OFFLINE']=='1'
