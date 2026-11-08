from hashlib import sha256
import time
from fastapi.testclient import TestClient
import pytest
from media_tag.api.app import create_app
from media_tag.web.http import COOKIE


def test_login_cookie_native_media_logout_and_private_headers(personnel):
    env=personnel
    with TestClient(env.app) as client:
        assert client.get('/ui/').status_code==200
        assert client.get('/ui/session').status_code==401
        login=client.post('/ui/session',headers={'Authorization':'Bearer owner','Origin':'http://testserver'})
        assert login.json()['actor_id']==env.owner.actor_id
        cookie=login.headers['set-cookie'].lower()
        assert 'httponly' in cookie and 'samesite=strict' in cookie and 'bearer' not in cookie
        assert client.get('/ui/library').status_code==200
        assert client.get('/ui/tasks').headers['cache-control']=='private, no-store'
        assert client.get('/ui/').headers['content-security-policy'].find("frame-ancestors 'none'")>=0
        assert client.delete('/ui/session',headers={'Origin':'http://testserver'}).status_code==204
        assert client.get('/ui/tasks').status_code==401


@pytest.mark.parametrize('headers',[{}, {'Origin':'http://evil.invalid'},{'Origin':'null'}, {'Origin':'http://testserver','Sec-Fetch-Site':'cross-site'}])
def test_cookie_writes_require_same_origin(personnel,headers):
    client=TestClient(personnel.app)
    client.post('/ui/session',headers={'Authorization':'Bearer owner'})
    assert client.post('/jobs',json={},headers=headers).status_code==403
    assert client.delete('/ui/session',headers=headers).status_code==403


def test_expired_forged_worker_and_disabled_sessions(personnel,monkeypatch):
    env=personnel
    assert env.clients.worker.post('/ui/session').status_code==403
    client=TestClient(env.app)
    client.cookies.set(COOKIE,'forged')
    assert client.get('/ui/session').status_code==401
    client.post('/ui/session',headers={'Authorization':'Bearer owner'})
    monkeypatch.setattr('media_tag.web.http.time.monotonic',lambda:time.time()+10**9)
    assert client.get('/ui/library').status_code==401
    app=create_app(env.router,{sha256(b'owner').hexdigest():env.owner})
    assert TestClient(app).get('/ui/').status_code==404


def test_static_allowlist_xss_boundaries_and_reader_permissions(personnel):
    env=personnel
    for name in ('app.js','sha256.js','style.css'):
        response=env.clients.owner.get('/ui/'+name)
        assert response.status_code==200
        assert response.headers['x-content-type-options']=='nosniff'
    result=env.clients.owner.get('/ui/app.js?name=../../catalog/database.py&mime=text/plain')
    assert result.content==env.clients.owner.get('/ui/app.js').content
    assert env.clients.owner.get('/ui/schema.py').status_code==404
    script=env.clients.owner.get('/ui/app.js').text
    assert '.innerHTML' not in script and 'localStorage.setItem(preferences(),value)' in script
    assert env.clients.reader.get('/ui/library').status_code==200
    assert env.clients.reader.get('/ui/tasks').status_code==403
