"""Single-stream PCM WAV sample clocks require exact real probe fields."""
from copy import deepcopy
from hashlib import sha256
from io import BytesIO
import json
import wave

import pytest

from precision.conftest import precise
from search.conftest import sought,env
from upload.conftest import tools
from media_tag.files.clock import pcm_wav_sample_clock
from media_tag.review.models import Observation
from media_tag.workers.probe import ProbeOptions,probe_media


def wav_bytes(channels=1,*,rate=8000,count=4000):
    raw=BytesIO()
    with wave.open(raw,'wb') as stream:
        stream.setnchannels(channels);stream.setsampwidth(2);stream.setframerate(rate)
        stream.writeframes(b'\0\0'*count*channels)
    return raw.getvalue()


@pytest.fixture
def pcm_metadata(tmp_path,tools):
    (tmp_path/'README.md').write_text('Synthetic exact PCM clock probe.\n')
    path=tmp_path/'pcm.wav';path.write_bytes(wav_bytes())
    return probe_media(path,tools,ProbeOptions()).as_dict()


def test_actual_probe_keeps_exact_sample_ticks_without_inventing_pts(pcm_metadata):
    stream=pcm_metadata['raw_metadata']['streams'][0]
    assert stream.get('start_time') is None and stream.get('start_pts') is None
    assert stream['duration_ts']==4000 and stream['time_base']=='1/8000'
    clock=pcm_wav_sample_clock(pcm_metadata)
    assert clock['normalization_origin_seconds']==0 and clock['duration_seconds']==.5
    assert clock['sample_count']==4000 and clock['sample_rate']==8000
    assert clock['duration_rational']=={'numerator':1,'denominator':2}
    assert clock['origin_basis']=='single_stream_integer_pcm_wav_sample_index'


@pytest.mark.parametrize('channels',[1,2])
def test_real_wav_audio_observation_uses_sample_frame_clock(precise,channels):
    p=precise;asset=p.register(wav_bytes(channels),'actual-'+str(channels)+'.wav','audio')
    reviews=p.sought.reviews
    with p.env.database.transaction() as connection:
        row=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset.asset_id,)).fetchone()
        technical=reviews.files._technical(connection,row)
    assert technical['start_seconds'] is None  # No fake container PTS.
    assert technical['time_basis']['sample_count']==4000  # Frames, not channels multiplied.
    receipt=reviews.observe(asset.asset_id,Observation(request_id='listen-real-wav',expected_revision=asset.revision,
        source_sha256=asset.sha256,kind='audio_segment',viewed=True,start_seconds=0,end_seconds=.5,
        note='Synthetic actual-file hearing observation, not business approval'),principal=p.env.owner)
    assert receipt['source_sha256']==asset.sha256
    with pytest.raises(ValueError,match='bounded interval'):
        reviews.observe(asset.asset_id,Observation(request_id='past-wav-end',expected_revision=asset.revision,
            source_sha256=asset.sha256,kind='audio_segment',viewed=True,start_seconds=0,end_seconds=.500125,
            note='Synthetic one-sample out-of-range observation must fail'),principal=p.env.owner)


@pytest.mark.parametrize('change',[
    lambda d:d['raw_metadata']['streams'][0].update(time_base='1/16000'),
    lambda d:d['raw_metadata']['streams'][0].update(time_base='0/0'),
    lambda d:d['raw_metadata']['streams'][0].pop('duration_ts'),
    lambda d:d['raw_metadata']['streams'][0].update(duration_ts=0),
    lambda d:d['raw_metadata']['streams'][0].update(duration_ts=True),
    lambda d:d['raw_metadata']['streams'][0].update(start_pts=3),
    lambda d:d['raw_metadata']['streams'][0].update(codec_name='pcm_f32le'),
    lambda d:d['raw_metadata']['streams'][0].update(codec_name='pcm_mulaw'),
    lambda d:d['raw_metadata']['streams'][0].update(codec_name='mp3'),
    lambda d:d['raw_metadata']['format'].update(format_name='unknown'),
    lambda d:d['raw_metadata']['format'].update(format_name='aiff'),
    lambda d:d['raw_metadata']['streams'].append(deepcopy(d['raw_metadata']['streams'][0])),
    lambda d:d.update(kind='video'),
])
def test_wrong_or_ambiguous_metadata_never_gets_a_sample_origin(pcm_metadata,change):
    data=deepcopy(pcm_metadata);change(data)
    assert pcm_wav_sample_clock(data) is None


def test_legacy_report_without_exact_ticks_keeps_actual_observation_closed(precise):
    p=precise;asset=p.register(wav_bytes(),'legacy.wav','audio');reviews=p.sought.reviews
    with p.env.database.transaction(write=True) as connection:
        attempt=connection.execute('''SELECT t.task_id,t.lease_epoch,a.result_json FROM router_tasks t
            JOIN router_jobs j USING(job_id) JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE j.asset_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')='probe' ''',(asset.asset_id,)).fetchone()
        result=json.loads(attempt['result_json']);ref=result['artifacts'][0];path=p.env.workspace/ref['ref']
        data=json.loads(path.read_bytes());data['raw_metadata']['streams'][0].pop('duration_ts')
        raw=json.dumps(data).encode();path.write_bytes(raw);ref['sha256']=sha256(raw).hexdigest()
        connection.execute('UPDATE router_attempts SET result_json=? WHERE task_id=? AND lease_epoch=?',
            (json.dumps(result),attempt['task_id'],attempt['lease_epoch']))
    with p.env.database.transaction() as connection:
        row=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset.asset_id,)).fetchone()
        technical=reviews.files._technical(connection,row)
    assert technical['time_basis']['normalization_origin_seconds'] is None
    assert technical['time_basis']['duration_seconds'] is None
    with pytest.raises(ValueError,match='known common source clock'):
        reviews.observe(asset.asset_id,Observation(request_id='no-guessed-legacy-origin',expected_revision=asset.revision,
            source_sha256=asset.sha256,kind='audio_segment',viewed=True,start_seconds=0,end_seconds=.5,
            note='Exact duration ticks are missing; decimal duration and prior scan do not substitute'),principal=p.env.owner)


def test_fractional_sample_duration_comes_from_ticks_not_rounded_seconds(tmp_path,tools):
    path=tmp_path/'fractional.wav';path.write_bytes(wav_bytes(rate=44100,count=22051))
    data=probe_media(path,tools,ProbeOptions()).as_dict();clock=pcm_wav_sample_clock(data)
    assert clock['sample_count']==22051 and clock['sample_rate']==44100
    assert clock['duration_seconds']==22051/44100
    assert clock['duration_seconds']!=float(data['raw_metadata']['streams'][0]['duration'])
