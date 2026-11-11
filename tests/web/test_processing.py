from hashlib import sha256
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from router.conftest import env
from upload.conftest import tools
from processing_queue.conftest import queue,register
from processing_queue.test_queue import run,submission

from media_tag.api.app import create_app
from media_tag.catalog import Principal
from media_tag.image_processing import ImageRecipe,MaskRegion
from media_tag.processing_queue import ImageSubmission,ProcessingService
from media_tag.processing_queue.derived import ProcessingDerivedService
from media_tag.processing_queue.derived_contracts import Reviewers


@pytest.fixture
def processing_ui(queue):
    e=queue.env;reviewer=Principal(actor_id='synthetic-output-reviewer')
    ids=(e.owner.actor_id,reviewer.actor_id)
    options=queue.options.model_copy(update={'reviewers':Reviewers(quality=ids,privacy=ids,watermark=ids)})
    queue.service.options=options;queue.handler.options=options
    derived=ProcessingDerivedService(e.router,queue.tools,options=options)
    def produce(request='ui-result'):
        source=register(queue,request+'.png')
        recipe=ImageRecipe(output_format='png',regions=(
            MaskRegion(region_id='face',purpose='face',box=(.2,.2,.6,.8),method='solid'),
            MaskRegion(region_id='watermark',purpose='watermark',box=(.7,.1,.9,.3),method='solid')))
        job,state=run(queue,submission(queue,source,recipe,ImageSubmission,request_id=request))
        return source,derived.adopt(job,principal=e.owner)
    source,output=produce()
    bindings={'owner':e.owner,'reviewer':reviewer,'other':Principal(actor_id='other'),'reader':Principal(actor_id=e.owner.actor_id,role='reader'),'worker':e.workers[0]}
    app=create_app(e.router,{sha256(token.encode()).hexdigest():actor for token,actor in bindings.items()},processing=queue.service,web=True)
    clients={token:TestClient(app,headers={'Authorization':'Bearer '+token}) for token in bindings}
    return SimpleNamespace(queue=queue,env=e,app=app,clients=clients,source=source,output=output,reviewer=reviewer,produce=produce,derived=derived)


def test_processing_ui_resources_bounded_pages_and_actual_capabilities(processing_ui):
    p=processing_ui;owner=p.clients['owner'];path='/ui/processing/outputs/'+p.output['asset_id']
    for url in ('/ui/processing','/ui/processing.js','/ui/processing.css'):
        response=owner.get(url);assert response.status_code==200 and response.headers['cache-control']=='private, no-store'
    detail=owner.get(path)
    assert detail.status_code==200,detail.text
    data=detail.json()
    assert data['can_compare'] and all(data['capabilities'][name]['can_approve'] for name in ('quality','privacy','watermark'))
    assert data['comparison']['source_sha256']==p.source.sha256 and data['comparison']['output_sha256']==p.output['output_sha256']
    assert 'frames' not in data['summary'] and str(p.env.workspace) not in detail.text
    assert not data['output_download_available'] and data['download_manifest'] is None
    assert TestClient(p.app).get(path).status_code==401
    for token in ('other','reader','worker'):assert p.clients[token].get(path).status_code==403
    assert p.clients['other'].get('/ui/processing/outputs').json()['items']==[]
    second_source,second=p.produce('ui-second')
    first=owner.get('/ui/processing/outputs?limit=1').json()
    next_page=owner.get('/ui/processing/outputs',params={'limit':1,'after':first['next']}).json()
    assert len(first['items'])==len(next_page['items'])==1
    assert first['items'][0]['asset_id']!=next_page['items'][0]['asset_id']
    assert owner.get('/ui/processing/outputs?limit=51').status_code==422
    assert owner.get('/ui/processing/outputs',params={'query':'x'*101}).status_code==422


def test_ui_capabilities_require_current_output_edit_and_source_read(processing_ui):
    p=processing_ui;e=p.env;path='/ui/processing/outputs/'+p.output['asset_id'];client=p.clients['reviewer']
    assert client.get(path).status_code==403
    output=e.catalog.change_access(p.output['asset_id'],principal=e.owner,expected_revision=p.output['revision'],subject_id=p.reviewer.actor_id,permission='read',reason='synthetic output read grant')
    data=client.get(path).json()
    assert not data['can_compare'] and not data['capabilities']['quality']['can_approve']
    e.catalog.change_access(p.source.asset_id,principal=e.owner,expected_revision=p.source.revision,subject_id=p.reviewer.actor_id,permission='read',reason='synthetic source read grant')
    data=client.get(path).json()
    assert data['can_compare'] and not data['capabilities']['quality']['can_approve']
    output=e.catalog.change_access(output.asset_id,principal=e.owner,expected_revision=output.revision,subject_id=p.reviewer.actor_id,permission='edit',reason='synthetic output edit grant')
    assert client.get(path).json()['capabilities']['quality']['can_approve']
    e.catalog.change_access(output.asset_id,principal=e.owner,expected_revision=output.revision,subject_id=p.reviewer.actor_id,permission='edit',reason='synthetic output edit revoke',revoke=True)
    data=client.get(path).json()
    assert not data['capabilities']['quality']['can_approve'] and not data['capabilities']['quality']['can_revoke']


def test_ui_default_no_reviewers_does_not_claim_assignment(processing_ui):
    p=processing_ui
    p.app.state.processing_derived.options=p.app.state.processing_derived.options.model_copy(update={'reviewers':Reviewers()})
    data=p.clients['owner'].get('/ui/processing/outputs/'+p.output['asset_id']).json()
    assert data['can_compare']
    assert all(not cap['configured'] and not cap['can_approve'] and not cap['can_revoke'] for cap in data['capabilities'].values())


@pytest.mark.parametrize('kind',['image','audio','video'])
def test_actual_chromium_processing_comparison_independent_review_and_revocation(processing_ui,tmp_path,kind):
    import json
    import os
    from pathlib import Path
    import shutil
    import socket
    import subprocess
    import threading
    import time
    import uvicorn
    from media_tag.workers.evidence import _directory
    p=processing_ui
    source,output=p.source,p.output
    if kind=='audio':
        from media_tag.cleaning import CleaningRecipe
        from media_tag.processing_queue import CleaningSubmission
        path=p.env.originals/'ui-audio.wav'
        p.queue.tools.ffmpeg_run(['-f','lavfi','-i','sine=frequency=440:duration=4',str(path)],timeout=20)
        source=register(p.queue,path.name)
        job,_=run(p.queue,submission(p.queue,source,CleaningRecipe(source_sha256=source.sha256,content_kind='audio'),CleaningSubmission,request_id='ui-audio'))
        output=p.derived.adopt(job,principal=p.env.owner)
    elif kind=='video':
        from media_tag.cleaning import CleaningRecipe
        from media_tag.cleaning.models import VideoSegment
        from media_tag.processing_queue import CleaningSubmission
        path=p.env.originals/'ui-video.mp4'
        p.queue.tools.ffmpeg_run(['-f','lavfi','-i','color=red:size=64x48:rate=10:duration=2',
            '-f','lavfi','-i','color=blue:size=64x48:rate=10:duration=4','-filter_complex',
            '[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]','-c:v','libx264','-threads','1',str(path)],timeout=20)
        source=register(p.queue,path.name)
        recipe=CleaningRecipe(source_sha256=source.sha256,content_kind='video',segment=VideoSegment(start_seconds=2,end_seconds=6),
            target_duration_seconds=4,preset='ultrafast')
        job,_=run(p.queue,submission(p.queue,source,recipe,CleaningSubmission,request_id='ui-video'))
        output=p.derived.adopt(job,principal=p.env.owner)
    chrome=Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    playwright=Path('C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright')
    if not chrome.is_file() or not playwright.is_dir() or not shutil.which('node'):pytest.skip('local Chromium/Playwright required')
    folder=_directory(p.env.workspace,'processing-browser')
    name='<img src=x onerror="window.__processing_xss=1">'
    p.env.catalog.revise(output['asset_id'],principal=p.env.owner,expected_revision=output['revision'],reason='synthetic XSS name test',name=name)
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128);port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(p.app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    worker=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);worker.start()
    try:
        deadline=time.monotonic()+15
        while not server.started:
            if time.monotonic()>deadline:raise RuntimeError('isolated UI server startup timeout')
            time.sleep(.05)
        context={'url':f'http://127.0.0.1:{port}','asset':output['asset_id'],'name':name,'sourceSha':source.sha256,'kind':kind,
            'outputSha':output['output_sha256'],'chrome':str(chrome),'playwright':str(playwright),
            'screenshot':str(folder/'comparison.png'),'result':str(folder/'browser-result.json')}
        path=folder/'context.json';path.write_text(json.dumps(context),encoding='utf8')
        result=subprocess.run(['node',str(Path(__file__).with_name('processing-browser.mjs')),str(path)],
            capture_output=True,text=True,timeout=90,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        assert result.returncode==0,result.stderr[-4000:]
        report=json.loads((folder/'browser-result.json').read_text())
        assert report['state']=='passed' and report['actual_chromium']
        print('Processing UI screenshot: '+str(folder/'comparison.png'))
    finally:
        server.should_exit=True;worker.join(15);sock.close()
