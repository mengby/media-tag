from hashlib import sha256
import json
import sqlite3
import subprocess
import sys
import pytest
import media_tag.catalog.database as module
from media_tag.catalog import Database,CatalogError
from .conftest import taxonomy,create


def test_api_auth_scope_body_limit_and_structured_errors(env):
    from fastapi.testclient import TestClient
    assert TestClient(env.app).get('/terminology/active').status_code==401
    assert env.clients['worker'].get('/terminology/active').status_code==403
    data={'request_id':'api-create','kind':'taxonomy','payload':taxonomy().model_dump(mode='json'),'note':'Synthetic API candidate'}
    response=env.clients['owner'].post('/terminology/candidates',json=data)
    assert response.status_code==200 and 'no-store' in response.headers['cache-control']
    cid=response.json()['candidate_id']
    assert env.clients['other'].get('/terminology/candidates/'+cid).status_code==403
    assert env.clients['owner'].post('/terminology/candidates/'+cid+'/publish',json={'request_id':'denied','expected_revision':1,'expected_generation':0,'version':'v1','note':'Synthetic'}).status_code==403
    response=env.clients['publisher'].post('/terminology/candidates/'+cid+'/publish',json={'request_id':'early','expected_revision':1,'expected_generation':0,'version':'v1','note':'Synthetic'})
    assert response.status_code==422 and response.json()['issues']==[{'code':'unreviewed_entries'}]
    response=env.clients['owner'].post('/terminology/candidates',json=data|{'kind':'not-a-kind','private-secret':'not echoed'})
    assert response.status_code==422 and 'not echoed' not in response.text
    assert env.clients['owner'].post('/terminology/candidates',content=b' '*1048577).status_code==413
    assert env.clients['owner'].get('/terminology/candidates/'+cid+'/preview').status_code==403


def test_cookie_csrf_and_disabled_by_default(env):
    from media_tag.api.app import create_app
    from fastapi.testclient import TestClient
    disabled=TestClient(create_app(env.router,{sha256(b'owner').hexdigest():env.owner}))
    assert disabled.get('/terminology/active').status_code==404
    browser=TestClient(env.app)
    assert browser.post('/ui/session',headers={'Authorization':'Bearer owner','Origin':'http://testserver'}).status_code==200
    assert browser.get('/terminology/active').status_code==200
    response=browser.post('/terminology/rollback',json={},headers={'Origin':'https://untrusted.invalid'})
    assert response.status_code==403


@pytest.mark.parametrize('name',['taxonomy','lexicon','candidate','revision','review','publish','rollback','import'])
def test_schema_real_project_interpreter(name):
    result=subprocess.run([sys.executable,'-m','media_tag','terminology-schema','--model',name],capture_output=True)
    assert result.returncode==0
    assert json.loads(result.stdout)['additionalProperties'] is False


def test_v6_to_v7_failure_rollback_preservation_and_checksums(tmp_path,monkeypatch):
    path=tmp_path/'synthetic-v6.sqlite3';c=sqlite3.connect(path)
    for version,statements in [(1,module.SCHEMA),(2,module.ROUTER_SCHEMA),(3,module.UPLOAD_SCHEMA),(4,module.REDACTION_SCHEMA),(5,module.LIBRARY_SCHEMA),(6,module.REVIEW_SCHEMA)]:
        for statement in statements:c.execute(statement)
        c.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(version,sha256('\n'.join(statements).encode()).hexdigest(),'synthetic'))
    c.execute("INSERT INTO catalog_metadata VALUES('synthetic','retained')");c.execute('PRAGMA user_version=6');c.commit();c.close()
    schema=module.TERMINOLOGY_SCHEMA;monkeypatch.setattr(module,'TERMINOLOGY_SCHEMA',(*schema,'invalid synthetic SQL'))
    with pytest.raises(sqlite3.OperationalError):Database(path).initialize()
    with Database(path).transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==6
        assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='terminology_candidates'").fetchone()
    monkeypatch.setattr(module,'TERMINOLOGY_SCHEMA',schema);Database(path).initialize();Database(path).initialize()
    with Database(path).transaction(write=True) as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==10
        assert c.execute('SELECT value FROM catalog_metadata').fetchone()[0]=='retained'
        c.execute("UPDATE schema_migrations SET checksum='tampered' WHERE version=7")
    with pytest.raises(CatalogError):Database(path).initialize()
