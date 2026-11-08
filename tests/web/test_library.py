from hashlib import sha256
import json
import pytest
from media_tag.catalog import AccessDenied, RevisionConflict, Principal
from media_tag.router.runtime import CPUHandlers, LocalExecutor
from media_tag.web.models import Enrollment
from media_tag.web.service import LibraryService
from upload.conftest import upload
from router.conftest import step, finish


def enroll(env,asset,**kwargs):
    data=Enrollment(request_id='synthetic-enroll',expected_revision=asset.revision,output_sha256=asset.sha256,**kwargs)
    return env.web.enroll(asset.asset_id,data,principal=env.owner)


def test_processing_upload_private_then_idempotent_enrollment_and_output_only_sharing(personnel):
    env=personnel
    done=upload(env,purpose='processing')
    asset=env.catalog.get_asset(done['asset_id'],principal=env.owner)
    assert env.web.assets(principal=env.owner)['items']==[]
    assert env.web.assets(principal=env.owner,workspace=True)['items']
    result=enroll(env,asset,recipients=(env.reader.actor_id,))
    again=enroll(env,asset,recipients=(env.reader.actor_id,))
    assert result==again and result['job_id']
    assert env.web.assets(principal=env.reader)['items'][0]['versions'][0]['asset_id']==asset.asset_id
    assert env.web.assets(principal=env.stranger)['items']==[]
    assert env.clients.reader.get(f'/ui/media/{asset.asset_id}?sha256={asset.sha256}').content==env.data
    assert env.clients.stranger.get(f'/ui/media/{asset.asset_id}?sha256={asset.sha256}').status_code==403
    assert env.clients.reader.post('/ui/library/'+asset.asset_id,json={'request_id':'reader','expected_revision':result['revision'],'output_sha256':asset.sha256}).status_code==403
    with env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM library_entries').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM library_events').fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM effective_values').fetchone()[0]==0
    current=env.catalog.get_asset(asset.asset_id,principal=env.owner)
    withdrawn=env.web.enroll(asset.asset_id,Enrollment(request_id='withdraw',expected_revision=current.revision,output_sha256=asset.sha256,active=False),principal=env.owner)
    assert not withdrawn['active'] and env.web.assets(principal=env.reader)['items']==[]
    assert env.clients.reader.get(f'/ui/media/{asset.asset_id}?sha256={asset.sha256}').status_code==403
    assert env.web.assets(principal=env.owner,workspace=True)['items']


def test_library_upload_listing_literal_query_pagination_and_no_automatic_share(personnel):
    env=personnel
    one=upload(env,request='one');two=upload(env,request='two')
    page=env.clients.owner.get('/ui/library?limit=1').json()
    assert len(page['items'])==1 and page['next']
    page2=env.clients.owner.get('/ui/library?limit=1&after='+page['next']).json()
    assert len(page2['items'])==1 and page2['items'][0]['root_asset_id']!=page['items'][0]['root_asset_id']
    assert env.clients.owner.get('/ui/library?q=missing').json()['items']==[]
    assert env.clients.reader.get('/ui/library').json()['items']==[]
    assert env.clients.owner.get('/ui/library?limit=1000').status_code==422
    assert env.clients.owner.get('/ui/library?q='+('x'*201)).status_code==422
    assert env.clients.owner.get('/ui/tasks').json()['uploads'][0]['filename']=='合成图片.png'


def test_stale_revision_hash_unknown_recipient_and_request_collision(personnel):
    env=personnel;done=upload(env,purpose='processing');asset=env.catalog.get_asset(done['asset_id'],principal=env.owner)
    for patch in ({'expected_revision':2},{'output_sha256':'0'*64},{'recipients':['unknown']}):
        response=env.clients.owner.post('/ui/library/'+asset.asset_id,json={'request_id':'bad','expected_revision':1,'output_sha256':asset.sha256,**patch})
        assert response.status_code in (409,412,422)
    enroll(env,asset)
    with pytest.raises(RevisionConflict):enroll(env,asset,recipients=(env.reader.actor_id,))
    assert env.clients.stranger.get('/ui/library/'+asset.asset_id).status_code==403


def test_enrollment_outbox_recovers_and_does_not_depend_on_tag_success(personnel,monkeypatch):
    env=personnel;done=upload(env,purpose='processing');asset=env.catalog.get_asset(done['asset_id'],principal=env.owner)
    submit=env.router.submit
    def unavailable(*a,**k):raise ValueError('synthetic dispatch failure')
    monkeypatch.setattr(env.router,'submit',unavailable)
    entry=enroll(env,asset)
    assert entry['dispatch_error']=='analysis_dispatch_unavailable' and entry['job_id'] is None
    monkeypatch.setattr(env.router,'submit',submit)
    recovered=LibraryService(env.router)
    recovered.dispatch_pending(principal=env.owner)
    entry=env.web.entry(asset.asset_id,principal=env.owner)
    assert entry['job_id']
    env.router.cancel(entry['job_id'],principal=env.owner)
    assert env.clients.owner.get(f'/ui/media/{asset.asset_id}?sha256={asset.sha256}&download=true').content==env.data
    assert env.catalog.get_asset(asset.asset_id,principal=env.owner).sha256==asset.sha256


def test_thumbnail_is_current_asset_evidence_and_revocation_is_live(personnel):
    env=personnel;done=upload(env);asset=env.catalog.get_asset(done['asset_id'],principal=env.owner)
    executor=LocalExecutor(env.router,env.workers[0],CPUHandlers(env.router,env.workers[0],env.uploads.tools))
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    enroll(env,asset,recipients=(env.reader.actor_id,))
    response=env.clients.reader.get(f'/ui/thumbnail/{asset.asset_id}?sha256={asset.sha256}')
    assert response.status_code==200 and response.content.startswith(b'\x89PNG')
    assert env.clients.reader.get(f'/ui/thumbnail/{asset.asset_id}?sha256='+('0'*64)).status_code==412
    asset=env.catalog.get_asset(asset.asset_id,principal=env.owner)
    env.web.enroll(asset.asset_id,Enrollment(request_id='withdraw',expected_revision=asset.revision,output_sha256=asset.sha256,active=False),principal=env.owner)
    assert env.clients.reader.get(f'/ui/thumbnail/{asset.asset_id}?sha256={asset.sha256}').status_code==403


def test_withdraw_does_not_remove_preexisting_grants(personnel):
    env=personnel;done=upload(env);asset=env.catalog.get_asset(done['asset_id'],principal=env.owner)
    env.catalog.change_access(asset.asset_id,principal=env.owner,expected_revision=1,subject_id=env.reader.actor_id,permission='read',reason='synthetic preexisting grant')
    asset=env.catalog.get_asset(asset.asset_id,principal=env.owner)
    enroll(env,asset,recipients=(env.reader.actor_id,))
    asset=env.catalog.get_asset(asset.asset_id,principal=env.owner)
    env.web.enroll(asset.asset_id,Enrollment(request_id='withdraw',expected_revision=asset.revision,output_sha256=asset.sha256,active=False),principal=env.owner)
    assert env.catalog.get_asset(asset.asset_id,principal=env.reader)
