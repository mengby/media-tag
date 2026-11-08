from hashlib import sha256
from pathlib import Path
from PIL import Image,ImageDraw
from fastapi.testclient import TestClient
from media_tag.review.models import FieldDecision,Observation
from media_tag.contracts.models import FieldValue
from media_tag.router.runtime import cpu_stages,CPUHandlers,LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.web.models import Enrollment
from media_tag.search.models import ConceptLink
from media_tag.workers.probe import Toolchain
from .test_http_pagination import more_assets


def client(sought,role='reader'):
    return TestClient(sought.app,headers={'Authorization':'Bearer '+role})


def run_cpu(sought):
    env=sought.env;tools=Toolchain();env.router.stages.update(cpu_stages(tools))
    env.router.register_worker(env.worker.actor_id,('probe','evidence'),principal=env.admin)
    sought.search.library.dispatch_pending(principal=env.owner)
    executor=LocalExecutor(env.router,env.worker,CPUHandlers(env.router,env.worker,tools))
    while (result:=executor.run_once()) is not None:assert result['accepted']
    return tools


def test_public_results_bind_fixed_versions_and_preserve_use_boundaries(sought):
    before=sought.env.catalog.get_asset(sought.asset.asset_id,principal=sought.env.owner)
    response=client(sought).post('/v1/assets/search',json={'query':'合成圆圈'})
    assert response.status_code==200 and 'no-store' in response.headers['cache-control']
    value=response.json();item=value['items'][0]
    assert value['schema_version']=='d17-search-v1' and value['limit']==10
    caps=client(sought).get('/v1/capabilities').json()
    assert caps['search_enabled'] and caps['read_only_identity'] and caps['bounded_playback_manifest']
    contract=client(sought).get(caps['search_contract']).json()
    assert contract['request_schema']['properties']['limit']['maximum']==50
    assert item['asset_id']==sought.asset.asset_id and item['sha256']==sought.asset.sha256 and item['revision']==before.revision
    assert item['download_manifest'].startswith('/v1/') and item['preview_manifest'].startswith('/v1/')
    assert item['fragments']==[] and item['current_use_review']=='not_checked' and not item['medical_use_approved']
    assert item['license_status']=='unverified'
    assert sought.env.catalog.get_asset(sought.asset.asset_id,principal=sought.env.owner).revision==before.revision
    assert client(sought).get(item['preview_manifest']+'&start_seconds=0&end_seconds=1').json()['error']['code']=='playback_clock_unavailable'
    assert not client(sought,'other').post('/v1/assets/search',json={'query':'合成圆圈'}).json()['items']
    assert client(sought,'worker').post('/v1/assets/search',json={'query':'合成圆圈'}).status_code==403


def test_public_schema_and_errors_are_bounded_and_typed(sought):
    reader=client(sought)
    for payload in ({'query':'x','user_id':'admin'},{'query':'x','limit':51},{'query':'x','filters':{'min_file_duration_seconds':3,'max_file_duration_seconds':1}},
        {'query':'x','filters':{'min_file_duration_seconds':float('inf')}},{'query':'x','schema_version':'unknown'}):
        if payload.get('filters',{}).get('min_file_duration_seconds')==float('inf'):
            response=reader.post('/v1/assets/search',content=b'{"query":"x","filters":{"min_file_duration_seconds":Infinity}}')
        else:response=reader.post('/v1/assets/search',json=payload)
        assert response.status_code==422 and response.json()['error']['code']=='invalid_request'
    assert reader.post('/v1/assets/search',content=b' '*1048577).json()['error']['code']=='request_body_too_large'
    assert TestClient(sought.app).post('/v1/assets/search',json={'query':'x'}).json()['error']['code']=='authentication_required'
    assert reader.post('/v1/assets/search',json={'query':'x','cursor':'forged'}).json()['error']['code']=='invalid_search_request'
    assert reader.post('/v1/assets/search',json={'query':'x','filters':{'usages':['invented-purpose']}}).status_code==422


def test_hard_filters_exclude_unknown_and_then_consume_real_probe_information(sought):
    owner=client(sought,'owner');spec={'query':'合成圆圈','filters':{'has_audio':False}}
    assert not owner.post('/v1/assets/search',json=spec).json()['items']
    run_cpu(sought)
    response=owner.post('/v1/assets/search',json=spec).json();assert response['items']
    technical=response['items'][0]['technical']
    assert technical['kind']=='image' and technical['display_width']==24 and technical['has_transparent_pixels'] is False
    filters={'media_kinds':['image'],'format_names':['png_pipe'],'min_display_width':24,'min_display_height':24,'has_transparent_pixels':False}
    assert owner.post('/v1/assets/search',json={'query':'合成圆圈','filters':filters}).json()['items']
    for extra in ({'media_kinds':['video']},{'format_names':['mov']},{'min_display_width':25},{'min_file_duration_seconds':0.0},{'has_transparent_pixels':True},{'require_redacted':True}):
        assert not owner.post('/v1/assets/search',json={'query':'合成圆圈','filters':filters|extra}).json()['items']
    filtered={'query':'合成圆圈','filters':{'usages':['信息图解']}}
    assert not owner.post('/v1/assets/search',json=filtered).json()['items']
    sought.reviews.decide(sought.asset.asset_id,FieldDecision(request_id='usage',expected_revision=sought.asset.revision,source_sha256=sought.asset.sha256,
        field='用途大类',value=FieldValue(state='observed',values=('信息图解',)),observation_ids=(sought.observation,),note='Synthetic geometry purpose'),principal=sought.env.owner)
    assert owner.post('/v1/assets/search',json=filtered).json()['items']


def test_public_cursor_binds_filters_intent_account_and_result_set(sought,tmp_path):
    more_assets(sought,tmp_path);owner=client(sought,'owner');spec={'query':'Page sample','use_model':False,'limit':1}
    first=owner.post('/v1/assets/search',json=spec).json();assert first['next_cursor']
    second=owner.post('/v1/assets/search',json=spec|{'cursor':first['next_cursor']}).json()
    assert first['items'][0]['asset_id']!=second['items'][0]['asset_id']
    for extra in ({'shot_intent':'different shot intent'},{'filters':{'exclude_asset_ids':[first['items'][0]['asset_id']]}}):
        response=owner.post('/v1/assets/search',json=spec|extra|{'cursor':first['next_cursor']})
        assert response.status_code==409 and response.json()['error']['code']=='search_results_changed'
    assert client(sought,'other').post('/v1/assets/search',json=spec|{'cursor':first['next_cursor']}).status_code==409


def test_human_effective_match_ranks_before_name_only_match(sought,tmp_path):
    env=sought.env;folder=tmp_path/'name-source';folder.mkdir();(folder/'README.md').write_text('Synthetic name-only source fixture.\n')
    path=folder/'name.png';Image.new('RGB',(10,10),'blue').save(path)
    other=env.catalog.register_file(FilesystemBackend(folder),path.name,source_backend_id='name-test',principal=env.owner,request_id='name-only',name='合成圆形 raw name')
    sought.search.library.enroll(other.asset_id,Enrollment(request_id='name-enroll',expected_revision=other.revision,output_sha256=other.sha256,active=True),principal=env.owner)
    result=client(sought,'owner').post('/v1/assets/search',json={'query':'合成圆形','use_model':False}).json()
    assert result['items'][0]['asset_id']==sought.asset.asset_id
    assert result['items'][0]['ranking_score']>result['items'][1]['ranking_score']


def test_public_search_rechecks_access_before_materializing_details(sought,monkeypatch):
    def changed(asset_id,**kwargs):
        from media_tag.files.service import FileError
        raise FileError(409,'privacy_review_revoked')
    monkeypatch.setattr(sought.search.files,'details',changed)
    response=client(sought,'owner').post('/v1/assets/search',json={'query':'合成圆圈'})
    assert response.status_code==409 and response.json()['error']['code']=='privacy_review_revoked'
    assert 'items' not in response.json()


def test_only_actual_continuous_link_intervals_are_returned_with_file_clock(sought,tmp_path):
    env=sought.env;tools=Toolchain();source=tmp_path/'video';source.mkdir();(source/'README.md').write_text('Generated growing-circle original media.\n')
    for number in range(8):
        image=Image.new('RGB',(96,64),'white');radius=4+number*3
        ImageDraw.Draw(image).ellipse((48-radius,32-radius,48+radius,32+radius),fill='red');image.save(source/f'frame-{number:02d}.png')
    video=source/'growing.mp4';tools.ffmpeg_run(['-v','error','-framerate','8','-i',str(source/'frame-%02d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(video)],timeout=20)
    asset=env.catalog.register_file(FilesystemBackend(source),video.name,source_backend_id='generated-video',principal=env.owner,request_id='video',name='Synthetic video')
    sought.search.library.enroll(asset.asset_id,Enrollment(request_id='video-enroll',expected_revision=asset.revision,output_sha256=asset.sha256,active=True),principal=env.owner)
    run_cpu(sought);asset=env.catalog.get_asset(asset.asset_id,principal=env.owner)
    observed=sought.reviews.observe(asset.asset_id,Observation(request_id='video-view',expected_revision=asset.revision,source_sha256=asset.sha256,
        kind='video_segment',start_seconds=.125,end_seconds=.75,viewed=True,note='Synthetic growing-circle protocol observation only; no business human review'),principal=env.owner)
    sought.search.links.create(asset.asset_id,ConceptLink(request_id='video-concept',expected_revision=asset.revision,source_sha256=asset.sha256,
        lexicon_release_id=sought.lex['release_id'],concept_id='concept-a',direction='increasing',observation_ids=(observed['observation_id'],),note='Synthetic geometric growth only'),principal=env.owner)
    owner=client(sought,'owner');result=owner.post('/v1/assets/search',json={'query':'合成圆圈','context':{'direction':'increasing'},'selected_concept_ids':['concept-a'],
        'selection_lexicon_release_id':sought.lex['release_id'],'filters':{'media_kinds':['video'],'min_file_duration_seconds':.9,'max_file_duration_seconds':1.1}}).json()
    assert len(result['items'])==1;item=result['items'][0];fragment=item['fragments'][0]
    assert item['asset_id']==asset.asset_id and fragment['sha256']==asset.sha256
    assert fragment['start_seconds']==.125 and fragment['end_seconds']==.75 and fragment['interval']=='half_open'
    assert item['technical']['frame_rate']==8.0 and item['technical']['video_codec']=='h264'
    manifest=owner.get(item['download_manifest']).json();raw=owner.get(manifest['content']).content
    assert sha256(raw).hexdigest()==asset.sha256 and manifest['whole_file']
    preview=owner.get(item['preview_manifest']+'&start_seconds=0.125&end_seconds=0.75').json()
    playback=preview['requested_playback'];assert playback['start_seconds']==.125 and playback['end_seconds']==.75
    assert playback['whole_file_bytes'] and not playback['server_clip_generated'] and not preview['full_video_observed']
    assert 'start_seconds=0.125' in preview['status_query']
    origin=playback['time_basis']['normalization_origin_seconds']
    assert all(.125<=r['source_seconds']-origin<.75 for r in preview['resources'] if r['kind']=='thumbnail')
    for interval in ('&start_seconds=0.5','&start_seconds=0.5&end_seconds=0.4','&start_seconds=0&end_seconds=2','&start_seconds=nan&end_seconds=1'):
        assert owner.get(item['preview_manifest']+interval).status_code==422
