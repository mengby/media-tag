"""Real HTTP and headless Chromium login; private synthetic empty catalogue only."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from uuid import uuid4
import httpx
import uvicorn
from media_tag.api.config import load_application
from media_tag.catalog.service import encode
from media_tag.contracts.templates import load_template_bundle
from media_tag.web.local_login import LocalBrowserLogin,TicketStore


def main():
    root=Path.cwd();folder=root/'jobs'/('local-login-'+uuid4().hex[:8]);folder.mkdir()
    (folder/'README.md').write_text('Private local-login HTTP/Chromium engineering checks. Empty catalogue, no medical or privacy approval.\n')
    for name in ('workspace','objects','browser-login'):
        p=folder/name;p.mkdir();(p/'README.md').write_text('Private local-login validation only.\n')
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128);port=sock.getsockname()[1];url=f'http://127.0.0.1:{port}'
    tokens={r:secrets.token_urlsafe(32) for r in ('owner','reader','worker')}
    config={'workspace':str(folder/'workspace'),'database':str(folder/'catalog.sqlite3'),'object_store':str(folder/'objects'),
        'template_dir':str(root/'asset-index-template'),'template_fingerprint':load_template_bundle(root/'asset-index-template').fingerprint,
        'credentials':{sha256(v.encode()).hexdigest():{'actor_id':'local-login-'+r,'role':'editor' if r=='owner' else r} for r,v in tokens.items()},
        'workers':['local-login-worker'],'web_enabled':True,'local_browser_login':{'origin':url,'directory':str(folder/'browser-login')}}
    path=folder/'runtime.local.json';path.write_text(encode(config));app,router,_=load_application(path)
    runtime=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    worker=threading.Thread(target=lambda:runtime.run(sockets=[sock]),daemon=True);worker.start()
    try:
        deadline=time.monotonic()+15
        while not runtime.started:
            if time.monotonic()>deadline:raise RuntimeError('server startup timeout')
            time.sleep(.05)
        store=TicketStore(LocalBrowserLogin.model_validate_json(encode(config['local_browser_login'])))
        context={'url':url,'ownerTicket':store.issue(sha256(tokens['owner'].encode()).hexdigest()),
            'readerTicket':store.issue(sha256(tokens['reader'].encode()).hexdigest()),
            'playwright':'C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright',
            'chrome':'C:/Program Files/Google/Chrome/Application/chrome.exe','screenshot':str(folder/'workspace.png'),'result':str(folder/'browser-result.json')}
        context_path=folder/'browser-context.local.json';context_path.write_text(encode(context))
        result=subprocess.run(['node',str(root/'tests/web/local-login-browser.mjs'),str(context_path)],capture_output=True,text=True,timeout=90,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'browser.stderr.log').write_text(result.stderr)
        assert result.returncode==0,'actual Chromium check failed; see private browser.stderr.log'
        assert all(v not in result.stdout+result.stderr for v in tokens.values())
        report=json.loads((folder/'browser-result.json').read_text())
        with httpx.Client(base_url=url) as client:
            assert client.get('/v1/capabilities').status_code==401
            assert client.get('/ui/session').status_code==401
        with router.database.transaction() as c:
            counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('assets','privacy_reviews','index_confirmations')}
        assert all(v==0 for v in counts.values())
        report.update(interpreter=sys.executable,business_acceptance=False,counts=counts)
        (folder/'validation.json').write_text(encode(report));print(encode({'state':'passed','report':str(folder/'validation.json')}));return 0
    finally:runtime.should_exit=True;worker.join(15);sock.close()


if __name__=='__main__':raise SystemExit(main())
