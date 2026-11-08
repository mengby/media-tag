from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlsplit,parse_qs
import json
import pytest
from test_local import initialized
from media_tag.deployment.browser import enable_browser,open_browser
from media_tag.deployment.local import DeploymentError,read_json,write_json


def test_launcher_keeps_long_lived_token_private_and_returns_clean_url(initialized,monkeypatch):
    root,_,_=initialized;before={p.name:sha256(p.read_bytes()).hexdigest() for p in root.glob('*token.local.txt')}
    enable_browser(root);captured=[];requests=[]
    monkeypatch.setattr('media_tag.deployment.browser.status',lambda root:{'running':True,'state':'running'})
    def opening(url,timeout):requests.append(url);return BytesIO(b'{"local_browser_login":true}')
    monkeypatch.setattr('urllib.request.build_opener',lambda *args:SimpleNamespace(open=opening))
    result=open_browser(root,opener=lambda url,**kwargs:captured.append(url) or True)
    assert result=={'state':'opened','url':'http://127.0.0.1:8765/ui/'}
    assert requests==['http://127.0.0.1:8765/ui/login-options']
    assert len(parse_qs(urlsplit(captured[0]).fragment)['local-login'][0])==43
    assert all(p.read_text() not in captured[0] for p in root.glob('*token.local.txt'))
    assert before=={p.name:sha256(p.read_bytes()).hexdigest() for p in root.glob('*token.local.txt')}


def test_launcher_rejects_reader_mapped_owner_file_and_instance_mismatch(initialized):
    root,_,_=initialized;enable_browser(root)
    config=read_json(root/'runtime.local.json');key=sha256((root/'owner-token.local.txt').read_bytes()).hexdigest()
    config['credentials'][key]['role']='reader';write_json(root,'runtime.local.json',config)
    with pytest.raises(DeploymentError,match='owner_identity_mismatch'):open_browser(root)


def test_launch_does_not_open_old_or_unready_api(initialized,monkeypatch):
    root,_,_=initialized;enable_browser(root)
    monkeypatch.setattr('media_tag.deployment.browser.status',lambda root:{'running':True,'state':'running'})
    monkeypatch.setattr('urllib.request.build_opener',lambda *args:SimpleNamespace(open=lambda *args,**kw:BytesIO(b'{"local_browser_login":false}')))
    with pytest.raises(DeploymentError,match='restart_local_service'):open_browser(root,opener=lambda *a,**kw:pytest.fail('must not open'))


def test_enable_refuses_to_change_a_running_legacy_config(initialized,monkeypatch):
    root,_,_=initialized;before=(root/'runtime.local.json').read_bytes()
    monkeypatch.setattr('media_tag.deployment.browser.status',lambda root:{'running':True})
    with pytest.raises(DeploymentError,match='stop_service_before'):enable_browser(root)
    assert (root/'runtime.local.json').read_bytes()==before and not (root/'browser-login').exists()
