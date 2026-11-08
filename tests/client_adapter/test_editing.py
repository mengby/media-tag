"""Editing orchestration boundaries with real fixed API/bytes; host checks have separate live validation."""
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import pytest
from media_tag.catalog.service import encode
from media_tag.client.editing import EditingSource
from media_tag.client.http import LibraryClientError
from media_tag.client.models import Acquisition
from test_provider import prepared


@pytest.fixture
def editing(prepared,tmp_path,monkeypatch):
    p=prepared;host=tmp_path/'host';(host/'jobs').mkdir(parents=True);assets=tmp_path/'external';assets.mkdir()
    source=object.__new__(EditingSource);source.host=host;source.assets=assets;source.jobs=host/'jobs'
    source.client=p.provider.client;source.tools=p.provider.tools
    source.init_job('new-job');task=source.task('new-job');edit=task/'edit'
    (edit/'automation/runs/test').mkdir(parents=True)
    transcript=b'{"synthetic":true}';candidate=b'{"media":[]}'
    (edit/'cut.transcript.json').write_bytes(transcript);(edit/'automation/runs/test/candidate.json').write_bytes(candidate)
    context=p.spec.context.model_copy(update={'candidate_path':'edit/automation/runs/test/candidate.json',
        'transcript_sha256':sha256(transcript).hexdigest(),'candidate_sha256':sha256(candidate).hexdigest()})
    spec=p.spec.model_copy(update={'context':context})
    def register(edit,identity,kind,src,page,note,**kwargs):
        asset={'id':identity,'type':kind,'src':src,'sha256':sha256((edit/src).read_bytes()).hexdigest(),
            'source_page':page,'candidate_note':note,'approval':{'status':'candidate'},**kwargs['extra']}
        (edit/'media_assets.json').write_text(encode({'transcript_sha256':context.transcript_sha256,'assets':[asset]}))
        return {'id':identity,'type':kind,'mode':'panel','src':src,'candidate':True}
    monkeypatch.setitem(sys.modules,'register_candidate',SimpleNamespace(register=register))
    monkeypatch.setitem(sys.modules,'media_privacy',SimpleNamespace(enabled=lambda:False,validate=lambda *args:None))
    value={'id':'illustration','acquisition':spec.model_dump(mode='json')}
    return SimpleNamespace(source=source,prepared=p,value=value,task=task,edit=edit)


def fetched(e):
    result=e.source.fetch('new-job',e.value)
    asset=json.loads((e.edit/'media_assets.json').read_text())['assets'][0]
    return result,{'item':result['item'],'asset':asset}


def test_existing_jobs_and_outside_ids_are_never_adopted(editing):
    e=editing
    with pytest.raises(LibraryClientError,match='existing_editing_job'):e.source.init_job('new-job')
    with pytest.raises(LibraryClientError,match='invalid_editing_job'):e.source.init_job('../old')
    (e.source.jobs/'old').mkdir()
    with pytest.raises(LibraryClientError,match='unmanaged_editing_job'):e.source.task('old')


def test_external_bytes_and_host_candidate_keep_distinct_provenance(editing):
    e=editing;result,value=fetched(e)
    assert Path(result['external_path']).is_relative_to(e.source.assets)
    assert Path(result['host_path']).is_relative_to(e.edit/'media')
    assert sha256(Path(result['host_path']).read_bytes()).hexdigest()==e.value['acquisition']['sha256']
    assert value['asset']['provider']=='media-tag' and value['asset']['approval']['status']=='candidate'
    assert result['final_export_allowed'] is False and e.source.validate('new-job',value)['state']=='valid'
    assert e.source.fetch('new-job',e.value)['reused'] is True
    changed=deepcopy(e.value);changed['acquisition']['context']['purpose']='A different use'
    with pytest.raises(LibraryClientError,match='already_registered'):e.source.fetch('new-job',changed)


def test_search_is_real_and_recorded_without_source_approval(editing):
    e=editing;result=e.source.search('new-job',{'query':e.value['acquisition']['asset_id'],'literal_only':True,'use_model':False})
    assert result['items'] and (e.task/result['search_record']).exists()
    assert not (e.edit/'media_assets.json').exists()


def test_document_mutation_blocks_fetch_before_host_registry(editing):
    e=editing;(e.edit/'automation/runs/test/candidate.json').write_text('{}')
    with pytest.raises(LibraryClientError,match='document_changed'):e.source.fetch('new-job',e.value)
    assert not (e.edit/'media_assets.json').exists()


def test_service_failure_is_not_an_empty_search(editing,monkeypatch):
    e=editing
    def fail(spec):raise LibraryClientError('upstream_unavailable',retryable=True)
    monkeypatch.setattr(e.source.client,'search',fail)
    with pytest.raises(LibraryClientError,match='upstream_unavailable'):
        e.source.search('new-job',{'query':'geometry','literal_only':True,'use_model':False})
    assert not (e.edit/'media-tag').exists()


def test_withdrawal_invalidates_already_copied_bytes(editing):
    e=editing;_,value=fetched(e)
    from test_provider import revoke
    revoke(e.prepared)
    with pytest.raises(LibraryClientError):e.source.validate('new-job',value)


@pytest.mark.parametrize('field',('src','start','sha256','source','source_records','source_page'))
def test_changed_host_binding_fails_closed(editing,field):
    e=editing;_,value=fetched(e);changed=deepcopy(value)
    if field=='src':changed['item']['src']='../raw/original.png'
    elif field=='start':changed['item']['start']=.1
    elif field=='sha256':changed['asset']['sha256']='0'*64
    elif field=='source_records':changed['asset']['source_records']=[{'label':'invented'}]
    elif field=='source_page':changed['asset']['source_page']='https://example.invalid/invented'
    else:changed['asset']['media_tag']['external_job_id']='other'
    with pytest.raises((LibraryClientError,ValueError)):e.source.validate('new-job',changed)
