from hashlib import sha256
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pydantic import ValidationError

from media_tag.api.config import LocalConfig, executor_from_config, load_application
from media_tag.catalog import Principal
from media_tag.cleaning import CleaningRecipe
from media_tag.cleaning.models import VideoSegment
from media_tag.image_processing import ImageRecipe, MaskRegion
from media_tag.processing_queue import ProcessingDeployment
from media_tag.processing_queue.derived_contracts import Reviewers
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import file_sha256


def auth(token='owner'):
    return {'Authorization':'Bearer '+token}


@pytest.fixture
def configured(env, tools, tmp_path, monkeypatch):
    import media_tag.api.config as module
    templates=tmp_path/'synthetic-template'
    templates.mkdir();(templates/'README.md').write_text('Synthetic contract loader for processing integration tests.\n')
    # Processing has no semantic target fields. Reuse the isolated three-field
    # contract rather than loading private original templates in this test.
    def bundle(path, *, expected_fingerprint):
        assert path==templates and expected_fingerprint==env.contract.fingerprint
        return env.contract
    monkeypatch.setattr(module,'load_template_bundle',bundle)
    config=LocalConfig(workspace=str(env.workspace),database=str(env.database.path),object_store=str(env.objects),
        template_dir=str(templates),template_fingerprint=env.contract.fingerprint,
        credentials={sha256(token.encode()).hexdigest():principal for token,principal in (
            ('owner',env.owner),('worker',env.workers[0]),('other',Principal(actor_id='other')),
            ('reader',Principal(actor_id=env.owner.actor_id,role='reader')))},
        workers=(env.workers[0].actor_id,),tool_dir=str(tools.ffmpeg.parent),
        processing=ProcessingDeployment(enabled=True,timeout_seconds=60))
    path=tmp_path/'synthetic.local.json'
    def boot(value=config):
        path.write_text(value.model_dump_json(),encoding='utf8')
        app,router,_=load_application(path)
        executor=executor_from_config(path,env.workers[0].actor_id)
        return SimpleNamespace(app=app,router=router,executor=executor,client=TestClient(app),config=value)
    result=boot();result.boot=boot;result.path=path;result.env=env;result.tools=tools
    return result


def register(configured, name='http.png'):
    c=configured
    path=c.env.originals/name
    if not path.exists():Image.new('RGBA',(48,32),(200,80,20,111)).save(path)
    return c.router.catalog.register_file(FilesystemBackend(c.env.originals),name,source_backend_id='synthetic-http',
        principal=c.env.owner,request_id=name,name=name)


def clean(asset,request='clean',**recipe):
    return {'request_id':request,'asset_id':asset.asset_id,'expected_revision':asset.revision,'source_sha256':asset.sha256,
        'recipe':CleaningRecipe(source_sha256=asset.sha256,content_kind='image',**recipe).model_dump(mode='json')}


def private_output(c,job):
    with c.router.database.transaction() as connection:
        raw=connection.execute('SELECT a.result_json FROM router_attempts a JOIN router_tasks t ON t.task_id=a.task_id WHERE t.job_id=? AND a.lease_epoch=t.lease_epoch',(job,)).fetchone()[0]
    artifacts=json.loads(raw)['artifacts']
    return c.env.workspace/next(item['ref'] for item in artifacts if item['kind']=='processing_output')


def status(c,job,token='owner'):
    return c.client.get('/processing/jobs/'+job,headers=auth(token))


def test_actual_local_config_http_cleaning_and_image_executor(configured):
    c=configured;asset=register(c)
    assert not c.config.analysis.enabled and not c.config.precision.enabled and not c.config.models.enabled
    assert {'cleaning_output','image_mask_output'}<=set(c.executor.router.stages)
    with c.router.database.transaction() as connection:
        stages=json.loads(connection.execute('SELECT stages_json FROM router_workers WHERE worker_id=?',(c.env.workers[0].actor_id,)).fetchone()[0])
        before=connection.execute('SELECT count(*) FROM assets').fetchone()[0]
    assert {'cleaning_output','image_mask_output'}<=set(stages)
    response=c.client.post('/processing/cleaning',json=clean(asset),headers=auth())
    assert response.status_code==200,response.text
    job=response.json()['job_id'];assert not response.json()['processing_completed']
    assert c.executor.run_once()['accepted']
    ready=status(c,job)
    assert ready.status_code==200 and ready.json()['state']=='pending_registration_review',ready.text
    assert file_sha256(private_output(c,job))==asset.sha256
    image_recipe=ImageRecipe(output_format='png',regions=(MaskRegion(region_id='face',purpose='face',box=(.2,.2,.8,.8),method='solid'),))
    image_body={key:value for key,value in clean(asset,'mask').items() if key!='recipe'}|{'recipe':image_recipe.model_dump(mode='json')}
    masked=c.client.post('/processing/images',json=image_body,headers=auth())
    assert masked.status_code==200,masked.text
    image_job=masked.json()['job_id'];assert c.executor.run_once()['accepted']
    image_status=status(c,image_job).json()
    assert image_status['state']=='pending_registration_review' and image_status['output_sha256']!=asset.sha256
    with Image.open(private_output(c,image_job)) as image:
        assert image.getpixel((24,16))==(0,0,0,111)
    assert file_sha256(c.env.originals/'http.png')==asset.sha256
    with c.router.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM assets').fetchone()[0]==before
    for result in (ready.json(),image_status):
        assert not result['asset_registered'] and not result['output_download_available']
        assert not result['privacy_confirmation'] and not result['quality_confirmation']
        assert 'ref' not in json.dumps(result) and str(c.env.workspace) not in json.dumps(result)
    assert ready.headers['cache-control']=='private, no-store'
    assert c.client.get('/processing/jobs/'+image_job+'/output',headers=auth()).status_code==404


def test_actual_http_video_crop_is_independent_of_semantic_stages(configured):
    c=configured;path=c.env.originals/'http.mp4'
    c.tools.ffmpeg_run(['-f','lavfi','-i','color=red:size=64x48:rate=10:duration=2',
        '-f','lavfi','-i','color=blue:size=64x48:rate=10:duration=4',
        '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','libx264','-threads','1',str(path)],timeout=20)
    asset=register(c,path.name)
    recipe=CleaningRecipe(source_sha256=asset.sha256,content_kind='video',segment=VideoSegment(start_seconds=2,end_seconds=6),target_duration_seconds=4,preset='ultrafast')
    body=clean(asset)|{'recipe':recipe.model_dump(mode='json')}
    response=c.client.post('/processing/cleaning',json=body,headers=auth())
    assert response.status_code==200,response.text
    job=response.json()['job_id'];assert c.executor.run_once()['accepted']
    result=status(c,job).json()
    assert result['state']=='pending_registration_review',result
    assert result['summary']['output_duration_seconds']==pytest.approx(4,abs=.1)
    raw,_=c.tools.ffmpeg_run(['-i',str(private_output(c,job)),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],timeout=10)
    assert raw[2]>200 and raw[0]<30
    assert file_sha256(path)==asset.sha256


def test_http_auth_strict_body_cas_and_cancel(configured,monkeypatch):
    c=configured;asset=register(c);body=clean(asset)
    assert c.client.post('/processing/cleaning',json=body).status_code==401
    for token in ('other','worker','reader'):
        assert c.client.post('/processing/cleaning',json=body,headers=auth(token)).status_code==403
    for extra in ({'path':'C:/private/source.png'},{'principal':{'actor_id':'synthetic-owner'}},{'limits':{'max_input_bytes':999}}):
        response=c.client.post('/processing/cleaning',json=body|extra,headers=auth())
        assert response.status_code==422 and 'C:/private' not in response.text
    assert c.client.post('/processing/cleaning',json=body|{'expected_revision':asset.revision+1},headers=auth()).status_code==409
    assert c.client.post('/processing/images',content=b'x'*(1024*1024+1),headers=auth()).status_code==413
    calls=[];original=c.app.state.processing.request
    def checked(*args,**kwargs):
        import threading
        calls.append(threading.current_thread().name)
        return original(*args,**kwargs)
    monkeypatch.setattr(c.app.state.processing,'request',checked)
    accepted=c.client.post('/processing/cleaning',json=body,headers=auth()).json()
    assert calls and calls[0]=='AnyIO worker thread'
    job=accepted['job_id']
    assert status(c,job,'other').status_code==403
    assert c.client.post('/jobs/'+job+'/cancel',headers=auth()).status_code==200
    assert c.executor.run_once() is None
    assert status(c,job).json()['state']=='cancelled'
    assert not list(c.env.workspace.glob('**/output.png'))


def test_disabled_configuration_never_registers_or_dispatches_processing(configured):
    c=configured
    disabled=c.boot(c.config.model_copy(update={'processing':ProcessingDeployment()}))
    assert disabled.app.state.processing is None
    assert not {'cleaning_output','image_mask_output'}&set(disabled.router.stages)
    assert disabled.client.post('/processing/cleaning',json={},headers=auth()).status_code==503
    assert disabled.client.get('/processing/jobs/job-not-present',headers=auth()).status_code==503
    assert disabled.client.post('/processing/images',json={}).status_code==401
    with pytest.raises(ValidationError):
        LocalConfig.model_validate_json(c.config.model_dump_json().replace('"enabled":true','"enabled":"yes"'))


def test_http_automatic_derived_adoption_private_comparison_and_separate_review(configured):
    c=configured;ids=(c.env.owner.actor_id,)
    enabled=c.config.model_copy(update={'processing':ProcessingDeployment(enabled=True,auto_adopt=True,timeout_seconds=60,
        reviewers=Reviewers(quality=ids,privacy=ids,watermark=ids))})
    boot=c.boot(enabled)
    c.app,c.router,c.executor,c.client=boot.app,boot.router,boot.executor,boot.client
    asset=register(c)
    recipe=ImageRecipe(output_format='png',regions=(
        MaskRegion(region_id='face',purpose='face',box=(.2,.2,.5,.8),method='solid'),
        MaskRegion(region_id='watermark',purpose='watermark',box=(.6,.1,.9,.3),method='solid')))
    payload=clean(asset)|{'recipe':recipe.model_dump(mode='json')}
    accepted=c.client.post('/processing/images',json=payload,headers=auth())
    assert accepted.status_code==200,accepted.text
    job=accepted.json()['job_id'];assert c.executor.run_once()['accepted']
    job_status=status(c,job).json()
    assert job_status['asset_registered'] and job_status['state']=='registered_pending_review',job_status
    output_id=job_status['derived_asset_id']
    registered=c.client.get('/processing/outputs/'+output_id,headers=auth()).json()
    assert registered['kind']=='derived' and registered['reviews']=={'quality':'pending','watermark':'pending','privacy':'pending'}
    assert c.client.post('/processing/jobs/'+job+'/adopt',headers=auth()).json()['asset_id']==output_id
    comparison=c.client.get('/processing/outputs/'+output_id+'/comparison',headers=auth())
    assert comparison.status_code==200,comparison.text
    original=c.client.get(comparison.json()['source_preview'],headers=auth())
    preview=c.client.get(comparison.json()['output_preview'],headers=auth())
    assert sha256(original.content).hexdigest()==asset.sha256
    assert sha256(preview.content).hexdigest()==registered['output_sha256']!=asset.sha256
    assert preview.headers['cache-control']=='private, no-store'
    assert c.client.get(comparison.json()['output_preview']).status_code==401
    assert c.client.get(comparison.json()['output_preview'],headers=auth('other')).status_code==403
    assert c.client.get(comparison.json()['output_preview'],headers=auth('reader')).status_code==403
    wrong=f'/processing/outputs/{output_id}/preview-content?sha256={asset.sha256}'
    assert c.client.get(wrong,headers=auth()).status_code==412
    ordinary=f'/v1/assets/{output_id}/content?sha256={registered["output_sha256"]}'
    assert c.client.get(ordinary,headers=auth()).status_code==409
    for dimension in ('quality','privacy','watermark'):
        spec={'expected_revision':registered['revision'],'output_sha256':registered['output_sha256'],'recipe_hash':registered['recipe_hash'],
            'dimension':dimension,'decision':'approved','full_output_viewed':True,'source_compared':True,
            'no_unintended_changes':True,'face_coverage_checked':True,'watermark_regions_checked':True}
        response=c.client.post('/processing/outputs/'+output_id+'/reviews',json=spec,headers=auth())
        assert response.status_code==200,response.text
        registered=response.json()
        if dimension!='watermark':assert not registered['output_download_available']
    assert registered['output_download_available'] and registered['download_manifest']
    manifest=c.client.get(registered['download_manifest'],headers=auth())
    assert manifest.status_code==200,manifest.text
    content=c.client.get(manifest.json()['content'],headers=auth())
    assert content.status_code==200 and sha256(content.content).hexdigest()==registered['output_sha256']
    assert status(c,job).json()['state']=='registered_available'
    revoke={'expected_revision':registered['revision'],'output_sha256':registered['output_sha256'],'recipe_hash':registered['recipe_hash'],
        'dimension':'watermark','decision':'revoked'}
    assert c.client.post('/processing/outputs/'+output_id+'/reviews',json=revoke,headers=auth()).status_code==200
    assert c.client.get(ordinary,headers=auth()).status_code==409
