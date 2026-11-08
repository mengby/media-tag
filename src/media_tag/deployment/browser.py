"""Current Windows user launches the browser using a private, short-lived handoff."""
from hashlib import sha256
import os
from pathlib import Path
import subprocess
import time
import webbrowser
from media_tag.catalog.service import encode
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.web.local_login import LocalBrowserLogin,TicketStore
from .local import DeploymentError,load,read_json,write_json,lock,status


def owner(root,instance):
    if os.name=='nt' and instance.windows_sid:
        import csv
        command=Path(os.environ['SystemRoot'])/'System32/whoami.exe'
        result=subprocess.run([str(command),'/user','/fo','csv','/nh'],capture_output=True,check=True,creationflags=subprocess.CREATE_NO_WINDOW)
        row=next(csv.reader(result.stdout.decode('utf8',errors='replace').strip().splitlines()))
        if instance.windows_sid not in row:raise DeploymentError('browser_entry_requires_instance_owner')
    with FilesystemBackend(root).open_read('owner-token.local.txt') as stream:raw=stream.read(4097)
    if len(raw)>4096:raise DeploymentError('invalid_local_owner_credential')
    digest=sha256(raw.decode('utf8').strip().encode()).hexdigest()
    config=read_json(root/'runtime.local.json');principal=config.get('credentials',{}).get(digest)
    if not principal or principal['actor_id']!=instance.owner_id or principal['role'] not in ('editor','admin'):
        raise DeploymentError('local_owner_identity_mismatch')
    return digest,config


def enable_browser(root):
    root,instance=load(root)
    with lock(root,name='configuration.lock'):
        _,config=owner(root,instance)
        if config.get('local_browser_login') is None and status(root)['running']:
            raise DeploymentError('stop_service_before_enabling_browser_login')
        folder=root/'browser-login'
        if not folder.exists():folder.mkdir()
        from .local import directory
        directory(folder)
        description=folder/'README.md'
        if not description.exists():description.write_text('Private OS-user browser handoff. SHA256-only single-use tickets expire in 60 seconds. Never share or commit.\n',encoding='utf8')
        browser=LocalBrowserLogin(origin=f'http://127.0.0.1:{instance.port}',directory=str(folder))
        config['local_browser_login']=browser.model_dump(mode='json')
        from media_tag.api.config import LocalConfig
        LocalConfig.model_validate_json(encode(config))
        write_json(root,'runtime.local.json',config)
    return {'state':'enabled','url':browser.origin+'/ui/','restart_required':True}


def open_browser(root,*,opener=None,timeout=30):
    root,instance=load(root);digest,config=owner(root,instance)
    if 'local_browser_login' not in config:raise DeploymentError('enable_local_browser_login_first')
    browser=LocalBrowserLogin.model_validate_json(encode(config['local_browser_login']))
    if browser.directory!=str(root/'browser-login') or browser.origin!=f'http://127.0.0.1:{instance.port}':raise DeploymentError('local_browser_instance_mismatch')
    state=status(root)
    if not state['running']:
        if os.name!='nt':raise DeploymentError('local_service_not_running')
        script=Path(instance.project_root)/'scripts/windows/local-service.ps1'
        command=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
        result=subprocess.run([str(command),'-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',str(script),'-Action','Start','-Root',str(root)],
            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise DeploymentError('local_service_start_failed')
    deadline=time.monotonic()+timeout
    while True:
        state=status(root)
        if state['running'] and state['state']=='running':break
        if time.monotonic()>=deadline:raise DeploymentError('local_browser_service_not_ready')
        time.sleep(.25)
    from urllib.request import build_opener,ProxyHandler
    from media_tag.client.http import NoRedirect
    try:
        with build_opener(ProxyHandler({}),NoRedirect()).open(browser.origin+'/ui/login-options',timeout=5) as response:
            import json
            if json.loads(response.read(1025)).get('local_browser_login') is not True:raise ValueError()
    except Exception:raise DeploymentError('restart_local_service_for_browser_login') from None
    ticket=TicketStore(browser).issue(digest)
    opened=(opener or webbrowser.open)(browser.origin+'/ui/#local-login='+ticket,new=2)
    if not opened:raise DeploymentError('local_browser_open_failed')
    return {'state':'opened','url':browser.origin+'/ui/'}
