import pytest

from media_tag.precision.runtime import evidence_window
from media_tag.models.contracts import ModelFailure


@pytest.mark.parametrize('audio', [False,True])
def test_precision_evidence_uses_actual_shared_file_clock(precise,tmp_path,audio):
    p=precise
    path=tmp_path/'shifted.mp4'
    p.tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=size=32x32:rate=5:duration=0.4',
        *(['-f','lavfi','-i','sine=frequency=440:duration=0.4','-map','0:v:0','-map','1:a:0','-c:a','aac'] if audio else []),
        '-vf','setpts=PTS+5/TB','-fps_mode','passthrough','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',
        '-video_track_timescale','1000',str(path)],timeout=20)
    asset=p.register(path.read_bytes(),'shifted.mp4','video')
    _,_,executor=p.submit(p.deployment(),asset_id=asset.asset_id)
    assert executor.run_once()['accepted']
    view=p.analysis.status(asset.asset_id,principal=p.env.owner)
    sample,=view['topic_analysis']['evidence']
    # Precision receives the whole file including audio. Its common range must
    # include both the early audio and late video, never just the first .4 sec.
    assert sample['start_seconds']==0.0
    assert sample['end_seconds']==pytest.approx(5.4 if audio else .4)


def test_missing_shared_stream_clock_does_not_guess_a_video_range():
    probe={'raw_metadata':{'streams':[{'codec_type':'video','start_time':'5','duration':'.4'},
        {'codec_type':'audio','duration':'.4'}]}}
    with pytest.raises(ModelFailure,match='precision_common_source_clock_required'):
        evidence_window(probe,'video')
