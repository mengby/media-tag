"""Current-user loopback deployment, owned process trees and conservative lease recovery."""
from contextlib import contextmanager,ExitStack
from hashlib import sha256
import csv
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4
from pydantic import Field
from typing import Annotated,Literal
from media_tag.contracts.models import ContractModel,Identifier,Sha256
from media_tag.catalog.service import encode
from media_tag.storage.filesystem import FilesystemBackend,_linked,_opened_path,require_local_path


class DeploymentError(Exception):pass


class Instance(ContractModel):
    schema_version: Literal['d07-local-v1']='d07-local-v1'
    instance_id: Annotated[str,Field(pattern='^[0-9a-f]{32}$')]
    project_root: str
    data_root: str
    host: Literal['127.0.0.1']='127.0.0.1'
    port: Annotated[int,Field(strict=True,ge=1024,le=65535)]
    worker_id: Identifier='local-worker'
    owner_id: Identifier='local-owner'
    reader_id: Identifier='local-codex-reader'
    windows_sid: str | None=None
    template_fingerprint: Sha256


def directory(path,*,existing=True):
    path=Path(path)
    if not path.is_absolute():raise DeploymentError('absolute_local_directory_required')
    require_local_path(path)
    for p in (path,*path.parents):
        if (p.exists() or p.is_symlink()) and _linked(p):raise DeploymentError('linked_deployment_path')
    if existing and not path.is_dir():raise DeploymentError('deployment_directory_missing')
    return path.resolve()


def read_json(path,limit=1024**2):
    path=Path(path);backend=FilesystemBackend(path.parent)
    if not path.exists() and not path.is_symlink():raise FileNotFoundError(path.name)
    with backend.open_read(path.name) as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise DeploymentError('deployment_file_byte_budget')
    return json.loads(raw)


def write_json(root,name,value):
    backend=FilesystemBackend(root,writable=True);target=backend._resolve(name,allow_missing=True)
    temporary=backend._resolve('write-'+uuid4().hex+'.tmp',allow_missing=True)
    try:
        with temporary.open('xb') as stream:
            if _opened_path(stream,temporary)!=temporary:raise DeploymentError('unsafe_deployment_handle')
            stream.write(encode(value).encode());stream.flush();os.fsync(stream.fileno())
        for attempt in range(100):
            try:os.replace(temporary,target);break
            except PermissionError:
                if attempt==99:raise
                time.sleep(.01)
    finally:temporary.unlink(missing_ok=True)


@contextmanager
def lock(root,*,name='run.lock'):
    backend=FilesystemBackend(root,writable=True);path=backend._resolve(name,allow_missing=True)
    try:
        with path.open('xb') as stream:stream.write(b'0')
    except FileExistsError:pass
    with path.open('r+b') as stream:
        if _opened_path(stream,path)!=path or os.fstat(stream.fileno()).st_nlink!=1:raise DeploymentError('unsafe_deployment_lock')
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:raise DeploymentError('deployment_already_running') from None
        try:yield
        finally:
            stream.seek(0)
            if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:fcntl.flock(stream,fcntl.LOCK_UN)


@contextmanager
def state_lock(root):
    for attempt in range(200):
        stack=ExitStack()
        try:stack.enter_context(lock(root,name='state.lock'))
        except DeploymentError as exc:
            stack.close()
            if str(exc)!='deployment_already_running':raise
            time.sleep(.01);continue
        try:yield
        finally:stack.close()
        return
    raise DeploymentError('deployment_state_busy')


def load(root):
    root=directory(root)
    instance=Instance.model_validate_json(encode(read_json(root/'instance.json')))
    if directory(instance.data_root)!=root:raise DeploymentError('deployment_root_mismatch')
    directory(instance.project_root)
    config=read_json(root/'runtime.local.json')
    if any(Path(config.get(key,'')).resolve()!=root/name for key,name in (('database','catalog.sqlite3'),('workspace','workspace'),('object_store','objects'))):
        raise DeploymentError('runtime_paths_outside_owned_instance')
    if config.get('workers')!=[instance.worker_id] or {p['actor_id'] for p in config.get('credentials',{}).values() if p['role']=='worker'}!={instance.worker_id}:
        raise DeploymentError('runtime_worker_identity_mismatch')
    return root,instance


def protect(root):
    if os.name!='nt':root.chmod(0o700);return None
    command=Path(os.environ['SystemRoot'])/'System32/whoami.exe'
    result=subprocess.run([str(command),'/user','/fo','csv','/nh'],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    row=next(csv.reader(result.stdout.decode('utf8',errors='replace').strip().splitlines()))
    sid=next((v for v in row if v.startswith('S-1-5-')),None)
    if not sid:raise DeploymentError('current_windows_sid_unavailable')
    result=subprocess.run([str(command.with_name('icacls.exe')),str(root),'/inheritance:r','/grant:r',f'*{sid}:(OI)(CI)F','*S-1-5-18:(OI)(CI)F'],capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise DeploymentError('private_deployment_acl_failed')
    return sid


def initialize(root,project,*,port=8765,vision=True):
    root=directory(root,existing=False);project=directory(project)
    if type(port) is not int or not 1024<=port<=65535:raise DeploymentError('invalid_local_port')
    if not root.is_relative_to(project/'data') or root==project/'data':raise DeploymentError('deployment_must_be_inside_project_data')
    if root.exists():
        _,instance=load(root)
        if Path(instance.project_root)!=project or instance.port!=port or read_json(root/'runtime.local.json')['models']['enabled']!=vision:
            raise DeploymentError('existing_deployment_configuration_mismatch')
        return {'state':'existing','instance_id':instance.instance_id,'url':f'http://127.0.0.1:{instance.port}/ui/'}
    from media_tag.contracts.templates import load_template_bundle
    from media_tag.workers.probe import Toolchain
    from media_tag.models.registry import ModelRegistry
    from media_tag.terminology.embedding.registry import BGERegistry
    contract=load_template_bundle(project/'asset-index-template')
    Toolchain(project/'cache/media-tools/ffmpeg-9.0.2/bin')
    if vision:
        registry=ModelRegistry(project/'cache/models/d04')
        for kind in ('ocr','face','clip'):registry.verify(kind)
    BGERegistry(project/'cache/models/d14').verify()
    root.parent.mkdir(exist_ok=True)
    if not (root.parent/'README.md').exists():(root.parent/'README.md').write_text('Private runtime data, credentials and local deployment; ignored by Git.\n',encoding='utf8')
    root.mkdir();sid=protect(root)
    (root/'README.md').write_text('Private current-user media-tag deployment. Tokens/config/database/logs are not Git artifacts. Originals are read only. Empty catalogue until explicit uploads/publication.\n',encoding='utf8')
    for name in ('workspace','objects','logs'):
        (root/name).mkdir();(root/name/'README.md').write_text('Private '+name+' for this local deployment only.\n',encoding='utf8')
    instance=Instance(instance_id=uuid4().hex,project_root=str(project),data_root=str(root),port=port,windows_sid=sid,template_fingerprint=contract.fingerprint)
    credentials={}
    for name,actor,role in (('owner',instance.owner_id,'editor'),('worker',instance.worker_id,'worker'),('reader',instance.reader_id,'reader')):
        token=secrets.token_urlsafe(32)
        with (root/(name+'-token.local.txt')).open('x',encoding='utf8') as stream:stream.write(token)
        if os.name!='nt':(root/(name+'-token.local.txt')).chmod(0o600)
        credentials[sha256(token.encode()).hexdigest()]={'actor_id':actor,'role':role}
    config={'workspace':str(root/'workspace'),'database':str(root/'catalog.sqlite3'),'object_store':str(root/'objects'),
        'template_dir':str(project/'asset-index-template'),'template_fingerprint':contract.fingerprint,'credentials':credentials,'workers':[instance.worker_id],
        'tool_dir':str(project/'cache/media-tools/ffmpeg-9.0.2/bin'),'models':{'enabled':vision,'model_root':str(project/'cache/models/d04'),'clip_device':'cuda'},
        'web_enabled':True,'review':{'enabled':True},'redaction':{'enabled':vision},'retime':{'enabled':True},
        'terminology':{'enabled':True,'managers':[instance.owner_id],'publishers':[instance.owner_id]},
        'text':{'enabled':True,'model_enabled':True,'model_root':str(project/'cache/models/d14')},'search':{'enabled':True}}
    from media_tag.api.config import LocalConfig
    LocalConfig.model_validate_json(encode(config))
    write_json(root,'runtime.local.json',config)
    write_json(root,'reader-client.local.json',{'base_url':f'http://127.0.0.1:{port}','token_file':str(root/'reader-token.local.txt')})
    write_json(root,'instance.json',instance.model_dump(mode='json'))
    return {'state':'initialized','instance_id':instance.instance_id,'url':f'http://127.0.0.1:{port}/ui/','catalogue_seeded':False,'windows_acl_restricted':sid is not None}


def requested(root,generation):
    try:return read_json(root/'stop.json').get('generation')==generation
    except FileNotFoundError:return False


def recover_owned(root,instance):
    # Called only after this instance's named OS job has no living members.
    from media_tag.api.config import load_application
    from media_tag.catalog import Principal
    app,router,_=load_application(root/'runtime.local.json')
    try:
        with router.database.transaction() as c:
            owned={(r['task_id'],r['lease_epoch']) for r in c.execute('SELECT task_id,lease_epoch FROM router_attempts WHERE worker_id=? AND released_at IS NULL',(instance.worker_id,))}
        router.process_stopped=lambda task,epoch:(task,epoch) in owned
        for task,epoch in owned:router.confirm_stopped(task,epoch,principal=Principal(actor_id='local-process-supervisor',role='admin'))
        return len(owned)
    finally:
        if getattr(app.state,'resolver',None):app.state.resolver.close()


def child(root,generation,kind):
    if sys.stdin.buffer.read(1)!=b'1':raise DeploymentError('missing_process_containment_gate')
    root,instance=load(root)
    os.chdir(instance.project_root)
    if kind=='api':
        from media_tag.api.config import load_application
        import uvicorn
        app,_,_=load_application(root/'runtime.local.json')
        server=uvicorn.Server(uvicorn.Config(app,host=instance.host,port=instance.port,access_log=False,log_level='error'))
        done=threading.Event()
        def stop_control():
            while not done.wait(.25):
                if requested(root,generation):server.should_exit=True;return
        control=threading.Thread(target=stop_control,daemon=True);control.start()
        try:server.run()
        finally:done.set();control.join()
    else:
        from media_tag.api.config import executor_from_config
        executor=executor_from_config(root/'runtime.local.json',instance.worker_id)
        while not requested(root,generation):
            if executor.run_once() is None:time.sleep(.5)


def resume_owned_process(process):
    """Resume the owned CREATE_SUSPENDED primary thread after assigning its job (including venv launchers)."""
    import ctypes as c
    from ctypes import wintypes as w
    class Entry(c.Structure):
        _fields_=[('size',w.DWORD),('usage',w.DWORD),('thread',w.DWORD),('owner',w.DWORD),('base',w.LONG),('delta',w.LONG),('flags',w.DWORD)]
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=(w.DWORD,w.DWORD);kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
    kernel.Thread32First.argtypes=(w.HANDLE,c.POINTER(Entry));kernel.Thread32First.restype=w.BOOL
    kernel.Thread32Next.argtypes=(w.HANDLE,c.POINTER(Entry));kernel.Thread32Next.restype=w.BOOL
    kernel.OpenThread.argtypes=(w.DWORD,w.BOOL,w.DWORD);kernel.OpenThread.restype=w.HANDLE
    kernel.ResumeThread.argtypes=(w.HANDLE,);kernel.ResumeThread.restype=w.DWORD
    kernel.CloseHandle.argtypes=(w.HANDLE,)
    handle=kernel.CreateToolhelp32Snapshot(4,0)
    if handle==w.HANDLE(-1).value:raise DeploymentError('child_thread_snapshot_failed')
    try:
        entry=Entry();entry.size=c.sizeof(entry);found=kernel.Thread32First(handle,c.byref(entry))
        while found:
            if entry.owner==process.pid:
                thread=kernel.OpenThread(2,False,entry.thread)
                if not thread:raise DeploymentError('child_thread_open_failed')
                try:
                    if kernel.ResumeThread(thread)==0xffffffff:raise DeploymentError('child_thread_resume_failed')
                    return
                finally:kernel.CloseHandle(thread)
            entry.size=c.sizeof(entry);found=kernel.Thread32Next(handle,c.byref(entry))
        raise DeploymentError('child_primary_thread_unavailable')
    finally:kernel.CloseHandle(handle)


def start_child(job,instance,root,generation,kind):
    process=subprocess.Popen([str(Path(instance.project_root)/'.venv/Scripts/python.exe'),'-m','media_tag','local-child',
        '--root',str(root),'--generation',generation,'--kind',kind],cwd=instance.project_root,stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW|0x00000004,env=os.environ.copy())
    try:
        job.assign(process);resume_owned_process(process);process.stdin.write(b'1');process.stdin.close();return process
    except BaseException:
        process.kill();process.wait(timeout=10)
        if process.stdin is not None:process.stdin.close()
        raise


def supervise(root):
    if os.name!='nt':raise DeploymentError('local_supervision_requires_windows')
    root,instance=load(root);os.chdir(instance.project_root)
    from media_tag.transforms.processes import ProcessTree
    from media_tag.client.http import LibraryClient,load_settings
    generation=uuid4().hex;children=[];job=None;owned=False
    logger=logging.getLogger('media-tag-local-'+instance.instance_id);logger.setLevel(logging.INFO);logger.propagate=False
    handler=RotatingFileHandler(root/'logs/supervisor.log',maxBytes=512*1024,backupCount=2,encoding='utf8');logger.addHandler(handler)
    def state(value,**extra):
        data={'schema_version':'d07-local-state-v1','generation':generation,'instance_id':instance.instance_id,'state':value,'supervisor_pid':os.getpid(),
            'child_pids':[p.pid for p in children],'updated_at':time.time(),**extra}
        write_json(root,'state.json',data);logger.info(encode({k:v for k,v in data.items() if k!='updated_at'}))
    def tree():return ProcessTree(name='Local\\media-tag-'+instance.instance_id)
    try:
        with ExitStack() as lifetime:
            with state_lock(root):
                lifetime.enter_context(lock(root));owned=True;state('starting')
            # Re-open the same named job after a crash; kill/wait for old descendants before releasing quota.
            tree().stop_and_close();recover_owned(root,instance)
            failures=0
            try:
                while not requested(root,generation):
                    with socket.socket() as probe:
                        try:probe.bind((instance.host,instance.port))
                        except OSError:raise DeploymentError('local_api_port_in_use') from None
                    job=tree();children=[]
                    for kind in ('api','worker'):
                        children.append(start_child(job,instance,root,generation,kind))
                    state('starting',restart_count=failures)
                    deadline=time.monotonic()+45;ready=False
                    while all(p.poll() is None for p in children) and not requested(root,generation):
                        if not ready:
                            try:
                                LibraryClient(load_settings(root/'reader-client.local.json')).require_reader();ready=True;state('running',restart_count=failures)
                            except Exception:
                                if time.monotonic()>deadline:break
                        time.sleep(.25 if not ready else 1)
                    if requested(root,generation):
                        state('stopping');limit=time.monotonic()+30
                        while any(p.poll() is None for p in children) and time.monotonic()<limit:time.sleep(.1)
                    job.stop_and_close();job=None
                    for p in children:p.wait(timeout=10)
                    recovered=recover_owned(root,instance)
                    if requested(root,generation):break
                    failures+=1;state('restarting',restart_count=failures,recovered_attempts=recovered)
                    if failures>=5:raise DeploymentError('local_restart_budget_exhausted')
                    for _ in range(20):
                        if requested(root,generation):break
                        time.sleep(.25)
            finally:
                if job is not None:
                    job.stop_and_close()
                    for p in children:p.wait(timeout=10)
                    recover_owned(root,instance)
            state('stopped');return 0
    except Exception as exc:
        if owned:state('failed',error=str(exc) if isinstance(exc,DeploymentError) else 'local_supervisor_failed')
        raise
    finally:handler.close();logger.removeHandler(handler)


def status(root):
    root,instance=load(root)
    with state_lock(root):
        try:
            with lock(root):running=False
        except DeploymentError as exc:
            if str(exc)!='deployment_already_running':raise
            running=True
        value=read_json(root/'state.json') if (root/'state.json').exists() else {'state':'not_started'}
    return value|{'running':running,'url':f'http://127.0.0.1:{instance.port}/ui/','instance_id':instance.instance_id}


def stop(root,*,timeout=45):
    root,_=load(root);value=status(root)
    if not value['running']:return {'state':'already_stopped'}
    write_json(root,'stop.json',{'generation':value['generation']})
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        if not status(root)['running']:return {'state':'stopped'}
        time.sleep(.1)
    raise DeploymentError('local_stop_timeout')
