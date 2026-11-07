from array import array
import json
import math
import wave
import pytest
from media_tag.transforms.models import RetimeSubmission
from .conftest import execute,register
from .test_retime import pcm


@pytest.mark.parametrize('kind,speed',[('vfr',.5),('vfr',2.),('rotated',1.25),('origin',2.),
    ('video-late',2.),('audio-late',.5),('multiple',2.),('drop',2.)])
def test_actual_vfr_rotation_origins_offsets_and_track_selection(retiming,tools,kind,speed):
    env=retiming; target=env.originals/f'synthetic-{kind}.mp4'
    if kind=='vfr':
        args=['-i',str(env.parent_path),'-vf',"select='not(mod(n,3))'",'-fps_mode','vfr','-c:v','libx264','-bf','0']
    elif kind=='rotated': args=['-display_rotation','90','-i',str(env.parent_path),'-c','copy']
    elif kind=='origin': args=['-i',str(env.parent_path),'-c','copy','-output_ts_offset','5']
    else:
        args=[*(['-itsoffset','0.4'] if kind=='video-late' else []),'-i',str(env.parent_path),
              *(['-itsoffset','0.3'] if kind=='audio-late' else []),'-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=2',
              '-map','0:v','-map','1:a']
        if kind=='multiple': args+=['-map','1:a']
        args+=['-c:v','copy','-c:a','aac']
    tools.ffmpeg_run(['-v','error',*args,str(target)],timeout=20);register(env,target)
    if kind=='multiple':
        with pytest.raises(ValueError,match='explicit output track'):
            env.retimes.start(RetimeSubmission(request_id='missing-selection',parent_asset_id=env.parent.asset_id,expected_revision=1),principal=env.owner)
    kwargs={'audio_stream_index':2} if kind=='multiple' else {'audio':'drop'} if kind=='drop' else {}
    _,status,asset,path,report=execute(env,speed=speed,**kwargs)
    info=tools.probe_run(path,timeout=10);video=next(s for s in info['streams'] if s['codec_type']=='video')
    assert report['full_output_decode'] and all(f['output_seconds']==pytest.approx(f['source_seconds']/speed) for f in report['frames'])
    if kind=='vfr': assert len({round(f['source_duration'],4) for f in report['frames']})>1
    if kind=='rotated': assert [video['width'],video['height']]==[64,96]
    if kind=='origin': assert report['time_map']['clock']['origin_seconds']==pytest.approx(5.)
    if kind=='video-late': assert float(video['start_time'])==pytest.approx(.4/speed,abs=.0001)
    if kind=='audio-late': assert report['audio'][0]['expected_start_seconds']>0
    if kind=='multiple': assert report['audio'][0]['source_stream_index']==2
    if kind=='drop': assert not report['audio'] and not any(s['codec_type']=='audio' for s in info['streams'])


@pytest.mark.parametrize('speed',[.5,2.,4.])
def test_decoded_audio_markers_scale_with_the_common_clock(retiming,tools,speed):
    env=retiming; signal=env.originals/'synthetic-markers.wav'
    values=array('h',(int(.7*32767*math.sin(2*math.pi*440*i/48000)) if .4<=i/48000<.6 or 1.4<=i/48000<1.6 else 0 for i in range(96000)))
    with wave.open(str(signal),'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(48000);output.writeframes(values.tobytes())
    target=env.originals/'synthetic-markers.mp4'
    tools.ffmpeg_run(['-v','error','-i',str(env.parent_path),'-itsoffset','0.25','-i',str(signal),'-map','0:v','-map','1:a',
        '-c:v','copy','-c:a','aac',str(target)],timeout=20);register(env,target)
    _,_,_,path,report=execute(env,speed=speed)
    def centers(file):
        data=pcm(tools,file);audio=next(s for s in tools.probe_run(file,timeout=10)['streams'] if s['codec_type']=='audio')
        start=float(audio['start_time']); result=[]
        for left,right in [(0.,1.25/speed),(1.25/speed,3./speed)]:
            pairs=[(i/48000+start,float(v)**2) for i,v in enumerate(data) if left<=i/48000+start<right]
            result.append(sum(t*e for t,e in pairs)/sum(e for t,e in pairs))
        return result
    original_pcm=pcm(tools,target);original_track=next(s for s in tools.probe_run(target,timeout=10)['streams'] if s['codec_type']=='audio')
    source_start=float(original_track['start_time']);source_centers=[]
    for left,right in [(0.,1.25),(1.25,3.)]:
        pairs=[(i/48000+source_start,float(v)**2) for i,v in enumerate(original_pcm) if left<=i/48000+source_start<right]
        source_centers.append(sum(t*e for t,e in pairs)/sum(e for t,e in pairs))
    measured=centers(path)
    assert measured==pytest.approx([t/speed for t in source_centers],abs=.035),(measured,source_centers,report['audio'])
