from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import secrets
from types import SimpleNamespace
import time
import pytest
from fastapi.testclient import TestClient
from media_tag.api.app import create_app
from media_tag.web.local_login import LocalBrowserLogin,TicketStore


@pytest.fixture
def local_login(personnel,tmp_path):
    directory=tmp_path/'private-login';directory.mkdir()
    config=LocalBrowserLogin(origin='http://127.0.0.1:8765',directory=str(directory))
    keys={role:sha256(secrets.token_bytes(32)).hexdigest() for role in ('owner','reader','worker')}
    credentials={keys['owner']:personnel.owner,keys['reader']:personnel.reader,keys['worker']:personnel.workers[0]}
    def application():return create_app(personnel.router,credentials,uploads=personnel.uploads,web=True,local_browser_login=config)
    app=application()
    def client(**kwargs):return TestClient(app,base_url=config.origin,client=('127.0.0.1',5000),**kwargs)
    return SimpleNamespace(config=config,keys=keys,credentials=credentials,app=app,application=application,client=client,store=TicketStore(config))


def redeem(e,ticket,client=None,**headers):
    return (client or e.client()).post('/ui/local-session',json={'ticket':ticket},headers={'Origin':e.config.origin,**headers})


def test_private_handoff_creates_existing_editor_session_without_bearer(local_login):
    e=local_login;c=e.client()
    assert c.get('/ui/session').status_code==401
    assert c.get('/v1/capabilities').status_code==401
    assert c.get('/ui/login-options').json()=={'local_browser_login':True}
    assert not c.cookies
    ticket=e.store.issue(e.keys['owner'])
    raw=(e.store.root/(sha256(ticket.encode()).hexdigest()+'.json')).read_text()
    assert ticket not in raw
    result=redeem(e,ticket,c)
    assert result.status_code==200 and result.json()['role']=='editor'
    assert result.json()['medical_review']=='not_assigned'
    cookie=result.headers['set-cookie'].lower()
    assert 'httponly' in cookie and 'samesite=strict' in cookie and ticket not in cookie
    assert c.get('/ui/tasks').status_code==200
    assert redeem(e,ticket).status_code==401
    assert c.delete('/ui/session',headers={'Origin':e.config.origin}).status_code==204
    assert c.get('/ui/tasks').status_code==401


@pytest.mark.parametrize('role,status',[('reader',200),('worker',403)])
def test_ticket_never_elevates_the_bound_identity(local_login,role,status):
    e=local_login;c=e.client();r=redeem(e,e.store.issue(e.keys[role]),c)
    assert r.status_code==status
    if role=='reader':
        assert r.json()['role']=='reader' and not any(r.json()['reviewer'].values())
        assert c.get('/ui/tasks').status_code==403
        assert c.post('/admin/dispatch',json={'paused':True},headers={'Origin':e.config.origin}).status_code==403
        assert c.post('/uploads',json={'request_id':'no-write','filename':'shape.png','size_bytes':10,'sha256':'0'*64},headers={'Origin':e.config.origin}).status_code==403
        assert c.get('/v1/capabilities').json()['read_only_identity'] is True


@pytest.mark.parametrize('headers',[{'Origin':'null'},{'Origin':'http://evil.invalid'},{'Sec-Fetch-Site':'cross-site'},
    {'Sec-Fetch-Site':'same-site'},{'Host':'evil.invalid:8765'},{'Host':'127.0.0.1:8766'},
    {'X-Forwarded-For':'127.0.0.1'},{'Forwarded':'for=127.0.0.1'},{'X-Forwarded-Host':'127.0.0.1:8765'}])
def test_wrong_origin_host_or_proxy_cannot_consume_ticket(local_login,headers):
    e=local_login;ticket=e.store.issue(e.keys['owner'])
    assert redeem(e,ticket,**headers).status_code==403
    assert redeem(e,ticket).status_code==200


def test_remote_peer_and_missing_origin_are_rejected(local_login):
    e=local_login;ticket=e.store.issue(e.keys['owner'])
    c=TestClient(e.app,base_url=e.config.origin,client=('192.0.2.1',5000))
    assert redeem(e,ticket,c).status_code==403
    assert e.client().post('/ui/local-session',json={'ticket':ticket}).status_code==403
    assert redeem(e,ticket).status_code==200


def test_expired_and_pre_restart_ticket_fail_closed(local_login,monkeypatch):
    e=local_login;ticket=e.store.issue(e.keys['owner']);future=time.time()+61
    with monkeypatch.context() as m:
        m.setattr('media_tag.web.local_login.time.time',lambda:future)
        assert redeem(e,ticket).status_code==401
    ticket=e.store.issue(e.keys['owner'])
    app=e.application();c=TestClient(app,base_url=e.config.origin,client=('127.0.0.1',5000))
    assert redeem(e,ticket,c).status_code==401


def test_concurrent_redemption_has_one_winner(local_login):
    e=local_login;ticket=e.store.issue(e.keys['owner'])
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:redeem(e,ticket).status_code,range(2)))
    assert sorted(results)==[200,401]


def test_unknown_credential_and_forged_ticket_do_not_authenticate(local_login):
    e=local_login
    assert redeem(e,secrets.token_urlsafe(32)).status_code==401
    assert redeem(e,e.store.issue('0'*64)).status_code==401
    assert e.client().post('/ui/local-session',json={'ticket':secrets.token_urlsafe(32),'actor_id':'admin'},headers={'Origin':e.config.origin}).status_code==422


def test_feature_remains_opt_in(personnel):
    assert personnel.clients.owner.get('/ui/login-options').json()=={'local_browser_login':False}
    assert personnel.clients.owner.post('/ui/local-session',json={'ticket':secrets.token_urlsafe(32)}).status_code==403
