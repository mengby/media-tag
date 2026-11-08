from hashlib import sha256
import json
from pathlib import Path
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.web.service import LibraryService
from media_tag.web.models import Enrollment
from media_tag.search.models import SearchRequest
from media_tag.terminology.resolve.models import ResolveInput


def more_assets(sought,tmp_path):
    env=sought.env;folder=tmp_path/'pages';folder.mkdir();assets=[]
    for number,color in enumerate(('blue','green','yellow')):
        path=folder/f'{number}.png';Image.new('RGB',(16+number,16),color).save(path)
        asset=env.catalog.register_file(FilesystemBackend(folder),path.name,source_backend_id='pages',principal=env.owner,request_id='page-'+str(number),name='Page sample '+str(number))
        LibraryService(env.router).enroll(asset.asset_id,Enrollment(request_id='page-enroll-'+str(number),expected_revision=asset.revision,output_sha256=asset.sha256,active=True),principal=env.owner)
        assets.append(env.catalog.get_asset(asset.asset_id,principal=env.owner))
    return assets


def test_cursor_paging_is_query_actor_and_dataset_bound(sought,tmp_path):
    assets=more_assets(sought,tmp_path);env=sought.env
    spec=SearchRequest(request=ResolveInput(query='Page sample',use_model=False),limit=1)
    first=sought.search.search(spec,principal=env.owner);assert first['next']
    second=sought.search.search(spec.model_copy(update={'cursor':first['next']}),principal=env.owner)
    assert first['items'][0]['root_asset_id']!=second['items'][0]['root_asset_id']
    with pytest.raises((ValueError,RevisionConflict)):sought.search.search(spec.model_copy(update={'cursor':first['next']+'bad'}),principal=env.owner)
    with pytest.raises(RevisionConflict):sought.search.search(spec.model_copy(update={'cursor':first['next']}),principal=env.other)
    with env.database.transaction(write=True) as c:c.execute('UPDATE assets SET revision=revision+1 WHERE asset_id=?',(assets[0].asset_id,))
    with pytest.raises(RevisionConflict):sought.search.search(spec.model_copy(update={'cursor':first['next']}),principal=env.owner)


def test_http_role_body_size_source_claims_and_cookie_origin(sought):
    client=TestClient(sought.app,headers={'Authorization':'Bearer owner'})
    result=client.post('/search/query',json={'request':{'query':'合成圆圈'}})
    assert result.status_code==200 and result.json()['items'] and 'no-store' in result.headers['cache-control']
    assert client.post('/search/query',json={'request':{'query':'合成圆圈','evidence_origin':'ocr'}}).status_code==422
    assert client.get('/search/management').status_code==403
    assert TestClient(sought.app).post('/search/query',json={'request':{'query':'x'}}).status_code==401
    assert TestClient(sought.app,headers={'Authorization':'Bearer worker'}).post('/search/query',json={'request':{'query':'x'}}).status_code==403
    assert client.post('/search/query',content=b' '*1048577).status_code==413
    manager=TestClient(sought.app,headers={'Authorization':'Bearer manager'})
    assert manager.get('/search/management').json()['capabilities']=={'manage':True,'publish':False}
    reader=TestClient(sought.app,headers={'Authorization':'Bearer reader'})
    assert reader.post('/search/query',json={'request':{'query':'合成圆圈'}}).json()['items']
    assert reader.post('/search/feedback',json={'request_id':'r','expression':'x'}).status_code==403
    browser=TestClient(sought.app)
    browser.post('/ui/session',headers={'Authorization':'Bearer owner','Origin':'http://testserver'})
    assert browser.post('/search/feedback',json={'request_id':'x','expression':'x'},headers={'Origin':'https://untrusted.invalid'}).status_code==403


def test_trial_uses_draft_words_but_only_authorized_assets(sought):
    from terminology.lexicon.conftest import create,alias
    from media_tag.terminology.contracts import Lexicon
    payload=sought.payload.model_dump(mode='json');payload['aliases'].append(alias('draft',expression='草稿圆圈'))
    candidate=create(sought.env,Lexicon.model_validate_json(json.dumps(payload)),base=sought.lex['release_id'],actor=sought.env.manager)
    manager=TestClient(sought.app,headers={'Authorization':'Bearer manager'})
    request={'expected_revision':1,'request':{'request':{'query':'草稿圆圈'}}}
    before=sought.env.service.active(principal=sought.env.owner)
    trial=manager.post('/search/management/candidates/'+candidate['candidate_id']+'/trial',json=request)
    assert trial.status_code==200 and not trial.json()['draft']['items'] and not trial.json()['online_changed']
    # Even a terminology manager must receive an explicit asset read grant.
    sought.env.catalog.change_access(sought.asset.asset_id,principal=sought.env.owner,expected_revision=sought.asset.revision,
        subject_id=sought.env.manager.actor_id,permission='read',reason='Synthetic explicit trial visibility')
    again=manager.post('/search/management/candidates/'+candidate['candidate_id']+'/trial',json=request).json()
    assert again['draft']['items'] and not again['published']['items']
    assert sought.env.service.active(principal=sought.env.owner)==before


def test_selected_candidate_binds_the_exact_word_release(sought):
    spec=SearchRequest(request=ResolveInput(query='多义说法'),selected_concept_ids=('concept-a',),selection_lexicon_release_id='stale')
    with pytest.raises(RevisionConflict):sought.search.search(spec,principal=sought.env.owner)
