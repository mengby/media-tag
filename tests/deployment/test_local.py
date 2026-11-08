from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import pytest
from media_tag.deployment.local import initialize,load,lock,status,stop,write_json,requested,DeploymentError


@pytest.fixture
def initialized(tmp_path,monkeypatch):
    from media_tag.contracts import templates
    from media_tag.contracts.templates import load_template_bundle
    from media_tag.workers.probe import Toolchain
    from media_tag.terminology.embedding.registry import BGERegistry
    bundle=load_template_bundle(Path('asset-index-template'))
    monkeypatch.setattr(templates,'load_template_bundle',lambda path:bundle)
    monkeypatch.setattr(Toolchain,'__init__',lambda self,directory:None)
    monkeypatch.setattr(BGERegistry,'verify',lambda self:None)
    project=tmp_path/'project';project.mkdir();root=project/'data/local'
    result=initialize(root,project,vision=False)
    return root,project,result


def test_private_initializer_is_empty_idempotent_and_never_rotates_tokens(initialized):
    root,project,result=initialized
    before={p.name:sha256(p.read_bytes()).hexdigest() for p in root.glob('*token.local.txt')}
    second=initialize(root,project,vision=False)
    assert second['state']=='existing' and second['instance_id']==result['instance_id']
    assert before=={p.name:sha256(p.read_bytes()).hexdigest() for p in root.glob('*token.local.txt')}
    config=json.loads((root/'runtime.local.json').read_text(encoding='utf8'))
    assert len(config['credentials'])==3 and not (root/'catalog.sqlite3').exists()
    assert config['review'].get('reviewers',[])==[] and config['terminology']['publishers']==['local-owner']
    assert len({p.read_text() for p in root.glob('*token.local.txt')})==3
    assert status(root)['state']=='not_started' and not status(root)['running']
    assert stop(root)['state']=='already_stopped'


def test_path_port_and_existing_configuration_cannot_be_silently_repurposed(initialized,tmp_path):
    root,project,_=initialized
    with pytest.raises(DeploymentError,match='configuration_mismatch'):initialize(root,project,port=9999,vision=False)
    with pytest.raises(DeploymentError,match='configuration_mismatch'):initialize(root,project,vision=True)
    with pytest.raises(DeploymentError,match='inside_project_data'):initialize(tmp_path/'outside',project,vision=False)
    with pytest.raises(DeploymentError,match='invalid_local_port'):initialize(project/'data/bad',project,port=1,vision=False)
    assert not (project/'data/bad').exists()
    with pytest.raises(DeploymentError,match='absolute'):initialize(Path('relative'),project,vision=False)


def test_lock_and_generation_scoped_stop_do_not_kill_unrelated_processes(initialized):
    root,_,result=initialized;generation='a'*32
    ready=threading.Event();finished=threading.Event()
    def owner():
        with lock(root):
            write_json(root,'state.json',{'generation':generation,'state':'running','instance_id':result['instance_id']});ready.set()
            while not requested(root,generation):time.sleep(.01)
        finished.set()
    thread=threading.Thread(target=owner);thread.start();assert ready.wait(3)
    assert status(root)['running']
    with pytest.raises(DeploymentError,match='already_running'):
        with lock(root):pass
    write_json(root,'stop.json',{'generation':'old'})
    assert not finished.wait(.05)
    assert stop(root,timeout=3)=={'state':'stopped'}
    thread.join(3);assert not thread.is_alive()


def test_child_refuses_execution_before_os_containment_gate(initialized):
    root,_,_=initialized
    run=subprocess.run([sys.executable,'-m','media_tag','local-child','--root',str(root),'--generation','x','--kind','api'],input=b'',capture_output=True,timeout=10)
    assert run.returncode==2 and json.loads(run.stderr)=={'error':'missing_process_containment_gate'}
    assert not (root/'catalog.sqlite3').exists()


def test_instance_metadata_cannot_point_to_another_root(initialized,tmp_path):
    root,_,_=initialized
    value=json.loads((root/'instance.json').read_text(encoding='utf8'));value['data_root']=str(tmp_path)
    write_json(root,'instance.json',value)
    with pytest.raises(DeploymentError,match='root_mismatch'):load(root)
    value['data_root']=str(root);write_json(root,'instance.json',value)
    config=json.loads((root/'runtime.local.json').read_text(encoding='utf8'));config['object_store']=str(tmp_path)
    write_json(root,'runtime.local.json',config)
    with pytest.raises(DeploymentError,match='outside_owned_instance'):load(root)


@pytest.mark.skipif(os.name!='nt',reason='Windows Job Object deployment only')
def test_venv_launcher_is_suspended_until_named_job_contains_actual_interpreter(tmp_path):
    from uuid import uuid4
    from media_tag.transforms.processes import ProcessTree
    from media_tag.deployment.local import resume_owned_process
    name='Local\\media-tag-test-'+uuid4().hex;tree=ProcessTree(name=name);marker=tmp_path/'actual-interpreter.json'
    code='''import ctypes as c,sys,json
from ctypes import wintypes as w
from pathlib import Path
k=c.WinDLL('kernel32',use_last_error=True)
k.OpenJobObjectW.argtypes=(w.DWORD,w.BOOL,w.LPCWSTR);k.OpenJobObjectW.restype=w.HANDLE
k.GetCurrentProcess.restype=w.HANDLE
k.IsProcessInJob.argtypes=(w.HANDLE,w.HANDLE,c.POINTER(w.BOOL));k.IsProcessInJob.restype=w.BOOL
k.CloseHandle.argtypes=(w.HANDLE,)
j=k.OpenJobObjectW(4,False,sys.argv[2]);inside=w.BOOL()
assert j and k.IsProcessInJob(k.GetCurrentProcess(),j,c.byref(inside))
Path(sys.argv[1]).write_text(json.dumps({'contained':bool(inside.value),'prefix':sys.prefix}))
k.CloseHandle(j)
'''
    p=subprocess.Popen([sys.executable,'-c',code,str(marker),name],creationflags=subprocess.CREATE_NO_WINDOW|4)
    try:
        tree.assign(p);time.sleep(.05);assert not marker.exists()
        resume_owned_process(p);assert p.wait(10)==0
        result=json.loads(marker.read_text());assert result['contained'] and Path(result['prefix'])==Path(sys.prefix)
    finally:tree.stop_and_close();p.wait(10)


@pytest.mark.skipif(os.name!='nt',reason='Windows Job Object deployment only')
def test_failed_job_assignment_reaps_owned_suspended_launcher(initialized):
    from media_tag.deployment.local import start_child
    root,_,_=initialized;_,instance=load(root)
    instance=instance.model_copy(update={'project_root':str(Path.cwd())})
    class Refused:
        def assign(self,p):self.process=p;raise DeploymentError('synthetic_job_assignment_refused')
    refused=Refused()
    with pytest.raises(DeploymentError,match='assignment_refused'):start_child(refused,instance,root,'x','api')
    assert refused.process.poll() is not None and not (root/'catalog.sqlite3').exists()


def test_status_never_reports_old_generation_during_supervisor_initialization(initialized):
    from contextlib import ExitStack
    from media_tag.deployment.local import state_lock
    root,_,_=initialized;entered=threading.Event();publish=threading.Event();finish=threading.Event();observed=threading.Event();result=[]
    write_json(root,'state.json',{'generation':'old','state':'running'})
    def initialize_owner():
        with ExitStack() as lifetime:
            with state_lock(root):
                lifetime.enter_context(lock(root));entered.set();assert publish.wait(3)
                write_json(root,'state.json',{'generation':'new','state':'starting'})
            finish.wait(3)
    def observe():result.append(status(root));observed.set()
    owner=threading.Thread(target=initialize_owner);reader=threading.Thread(target=observe)
    owner.start();assert entered.wait(3)
    try:
        reader.start();assert not observed.wait(.05)
        publish.set();assert observed.wait(3)
        assert result[0]['generation']=='new' and result[0]['running'] and result[0]['state']=='starting'
    finally:publish.set();finish.set();owner.join(3);reader.join(3)
