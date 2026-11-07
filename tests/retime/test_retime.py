from array import array
from hashlib import sha256
import json
import math
import pytest
from router.test_api import TestClient
from media_tag.api.app import create_app
from media_tag.catalog import Principal
from media_tag.transforms.models import RetimeSubmission
from media_tag.transforms.retime import tempo_chain
from media_tag.variants.models import SourceSegment
from .conftest import execute,register


@pytest.mark.parametrize('speed',[.25,.5,1.,1.25,2.,4.])
def test_real_video_speed_preserves_all_frames_and_source(retiming,tools,speed):
    env=retiming; original=env.parent_path.read_bytes(); before=env.parent
    submitted,status,asset,path,report=execute(env,speed=speed,segments=(SourceSegment(segment_id='synthetic-cue',start_seconds=.2,end_seconds=1.5),))
    info=tools.probe_run(path,timeout=10)
    assert float(info['streams'][0]['duration'])==pytest.approx(2/speed,abs=.0001)
    assert report['decoded_frame_count']==20 and not report['interpolation'] and report['video_encode_passes']==1
    assert all(f['output_seconds']==pytest.approx(f['source_seconds']/speed) for f in report['frames'])
    assert report['segments'][0]['output_end_seconds']==pytest.approx(1.5/speed)
    assert env.parent_path.read_bytes()==original and env.catalog.get_asset(before.asset_id,principal=env.owner)==before
    assert status['privacy_review']=='not_applicable' and report['peak_process_bytes']>0


def pcm(tools,path):
    raw,_=tools.ffmpeg_run(['-v','error','-i',str(path),'-map','0:a:0','-ac','1','-ar','48000','-f','f32le','pipe:1'],timeout=20)
    values=array('f'); values.frombytes(raw)
    return values


@pytest.mark.parametrize('speed',[.25,.5,1.25,2.,4.])
def test_actual_audio_tempo_preserves_pitch(retiming,tools,speed):
    env=retiming; path=env.originals/'synthetic-tone.mp4'
    tools.ffmpeg_run(['-v','error','-i',str(env.parent_path),'-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2',
        '-map','0:v','-map','1:a','-c:v','copy','-c:a','aac',str(path)],timeout=20)
    register(env,path)
    _,_,_,result,report=execute(env,speed=speed)
    values=pcm(tools,result); end=min(.8,len(values)/48000-.05); start=.05
    samples=values[int(start*48000):int(end*48000)]
    positive_crossings=sum(a<=0<b for a,b in zip(samples,samples[1:]))
    frequency=positive_crossings/((len(samples)-1)/48000)
    assert frequency==pytest.approx(440,abs=8),frequency
    assert len(values)/48000==pytest.approx(2/speed,abs=.03)
    assert report['audio'][0]['tempo_filter']==tempo_chain(speed)


def test_default_repeat_changed_speed_and_explicit_chain_never_implicitly_stack(retiming):
    env=retiming
    submitted,status,asset,path,report=execute(env,speed=2.,segments=(SourceSegment(segment_id='cue',start_seconds=.4,end_seconds=1.6),))
    repeated=env.retimes.start(RetimeSubmission(request_id='other-request',parent_asset_id=env.parent.asset_id,expected_revision=1,speed=2.,
        segments=(SourceSegment(segment_id='cue',start_seconds=.4,end_seconds=1.6),)),principal=env.owner)
    assert repeated['job_id']==submitted['job_id'] and repeated['asset_id']==asset.asset_id and env.executor.run_once() is None
    default=env.retimes.start(RetimeSubmission(request_id='default',parent_asset_id=env.parent.asset_id,expected_revision=1),principal=env.owner)
    assert default['time_map']['speed']==1.
    assert env.executor.run_once()['accepted']
    changed=env.retimes.start(RetimeSubmission(request_id='changed',parent_asset_id=env.parent.asset_id,expected_revision=1,speed=1.25),principal=env.owner)
    assert changed['cumulative_speed']==1.25
    assert env.executor.run_once()['accepted']
    with pytest.raises(ValueError,match='explicit'):
        env.retimes.start(RetimeSubmission(request_id='implicit',parent_asset_id=asset.asset_id,expected_revision=1,speed=2.),principal=env.owner)
    chained=env.retimes.start(RetimeSubmission(request_id='explicit',parent_asset_id=asset.asset_id,expected_revision=1,speed=2.,chain_from_derived=True),principal=env.owner)
    assert chained['cumulative_speed']==4 and chained['segments'][0]['output_start_seconds']==pytest.approx(.1)
    assert env.executor.run_once()['accepted']
    assert env.retimes.get(chained['job_id'],principal=env.owner)['asset_id']


@pytest.mark.parametrize('payload',[{'speed':True},{'speed':0.},{'speed':5.},{'speed':float('nan')},{'ffmpeg':'injected'},{'options':{'score_threshold':.5}}])
def test_retime_input_is_strict_and_does_not_accept_model_settings(payload):
    with pytest.raises(ValueError): RetimeSubmission.model_validate_json(json.dumps(dict(request_id='bad',parent_asset_id='synthetic',expected_revision=1)|payload))


def test_http_ownership_readonly_and_default_speed(retiming):
    env=retiming
    bindings={'owner':env.owner,'worker':env.workers[0],'other':Principal(actor_id='synthetic-other'),'reader':Principal(actor_id=env.owner.actor_id,role='reader')}
    app=create_app(env.router,{sha256(k.encode()).hexdigest():p for k,p in bindings.items()},retiming=env.retimes)
    auth=lambda key:{'Authorization':'Bearer '+key}
    data={'request_id':'http','parent_asset_id':env.parent.asset_id,'expected_revision':1}
    with TestClient(app) as client:
        assert client.post('/retime/jobs',json=data).status_code==401
        assert client.post('/retime/jobs',headers=auth('worker'),json=data).status_code==403
        assert client.post('/retime/jobs',headers=auth('reader'),json=data).status_code==403
        response=client.post('/retime/jobs',headers=auth('owner'),json=data); assert response.status_code==200
        job=response.json(); assert job['time_map']['speed']==1 and response.headers['cache-control']=='private, no-store'
        assert client.get('/retime/jobs/'+job['job_id'],headers=auth('other')).status_code==403
        assert env.executor.run_once()['accepted']
        done=client.get('/retime/jobs/'+job['job_id'],headers=auth('owner')).json()
        assert done['asset_id'] and done['job']['state']=='succeeded'
