from hashlib import sha256
from fractions import Fraction
from io import BytesIO
import json
from pathlib import Path

from PIL import Image
import pytest

from search.conftest import sought,env
from upload.conftest import tools
from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.temporal_topics import Generation,LocalQwenVideoGenerator,Options,Pending,TemporalTopicService
from media_tag.temporal_topics.contracts import Sequence,Rational,Result
from media_tag.temporal_topics.decode import grid
from media_tag.temporal_topics.native import input_digest
from media_tag.temporal_topics.prompt import PROMPT_VERSION
from media_tag.storage.filesystem import FilesystemBackend


class MockVideo:
    model_revision='a'*64
    def __init__(self):self.calls=[];self.change={}
    def __call__(self,**kwargs):
        self.calls.append(kwargs)
        answers=[]
        for field in TOPIC_FIELDS:
            values=['left to right'] if field=='英文检索词' else ['未发布过程→候选'] if field=='过程/状态' else []
            answers.append(json.dumps({'field':field,'state':'observed' if values else 'unknown',
                'values':values,'dynamic':bool(values)},ensure_ascii=False))
        payload=dict(backend='synthetic_protocol',model_revision=self.model_revision,prompt_version=PROMPT_VERSION,
            input_sha256=kwargs['input_sha256'],answers=tuple(answers));payload.update(self.change)
        return Generation(**payload)


@pytest.fixture
def continuous(sought,tmp_path):
    source=tmp_path/'temporal-sources';source.mkdir();(source/'README.md').write_text('Synthetic only.')
    workspace=tmp_path/'temporal-drafts';workspace.mkdir()
    return sought,source,workspace


def run(continuous,path,kind,*,options=None,tools=None,generator=None):
    sought,source,workspace=continuous
    generator=generator or MockVideo()
    service=TemporalTopicService(workspace,policy=sought.reviews.policy,generator=generator,tools=tools,options=options)
    result=service.run(FilesystemBackend(source),path.name,expected_source_sha256=sha256(path.read_bytes()).hexdigest(),
        asset_id=sought.asset.asset_id,asset_revision=sought.asset.revision,analysis_id='temporal-test',content_kind=kind)
    return service,result,generator


def gif(path):
    Image.new('RGBA',(32,32),'red').save(path,save_all=True,append_images=[Image.new('RGBA',(32,32),'blue')],
        duration=[80,120],loop=0)


def test_gif_all_frames_exact_grid_dynamic_evidence_and_no_human_write(continuous):
    sought,source,workspace=continuous;path=source/'unrelated-private-name.gif';gif(path)
    original=path.read_bytes();before=sought.reviews.detail(sought.asset.asset_id,principal=sought.env.owner)
    service,result,generator=run(continuous,path,'animation')
    sequence=result.sequence
    assert sequence.coverage=='one_complete_animation_cycle' and sequence.animation_plays==0
    assert [f.duration.fraction() for f in sequence.frames]==[Fraction(2,25),Fraction(3,25)]
    assert sequence.model_frame_sources==(0,0,1,1,1) and sequence.model_fps.fraction()==25
    assert len(generator.calls[0]['frame_bytes'])==5 and sequence.duration.fraction()==Fraction(1,5)
    assert tuple(f.field for f in result.document.fields)==TOPIC_FIELDS
    fields={f.field:f for f in result.document.fields}
    assert fields['英文检索词'].dynamic and fields['英文检索词'].value.state=='observed'
    assert fields['过程/状态'].dynamic and fields['过程/状态'].value.state=='observed'
    assert fields['过程/状态'].next_step=='human' and fields['过程/状态'].requires_field_review
    assert result.document.evidence[0].kind=='video_segment'
    assert result.document.evidence[0].end_seconds==.2
    assert not result.privacy_confirmation and not result.effective_values_written and not result.precision_exemption
    assert result.backend=='synthetic_protocol' and path.read_bytes()==original
    assert sought.reviews.detail(sought.asset.asset_id,principal=sought.env.owner)==before
    with service.backend.open_read(result.raw_report.ref) as stream:report=json.load(stream)
    assert report['policy_digest']==sought.reviews.policy.digest and report['audio_policy']=='not_analyzed'
    assert path.name not in json.dumps(generator.calls[0]['field_prompts'],ensure_ascii=False)


def test_apng_rational_timing_poster_is_excluded(continuous):
    sought,source,workspace=continuous;path=source/'poster.png'
    from media_tag.image_processing.animation import encode,Decoded
    frames=tuple(Image.new('RGBA',(32,32),c) for c in ('white','red','blue'))
    decoded=Decoded(frames,(None,Fraction(1,60),Fraction(1,30)),('poster','animation','animation'),2,True,'PNG',None)
    path.write_bytes(encode(frames,decoded,'apng',1024**2))
    service,result,generator=run(continuous,path,'animation')
    assert result.sequence.excluded_poster_rgba_sha256 is not None
    assert len(result.sequence.frames)==2 and result.sequence.animation_plays==2
    assert [f.duration.fraction() for f in result.sequence.frames]==[Fraction(1,60),Fraction(1,30)]
    assert result.sequence.model_frame_sources==(0,1,1) and result.sequence.model_fps.fraction()==60
    assert sum((f.duration.fraction() for f in result.sequence.frames),Fraction(0))==result.sequence.duration.fraction()


def test_continuous_video_nonzero_selected_pts(continuous,tools):
    sought,source,workspace=continuous;path=source/'synthetic.mp4'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=size=32x32:rate=5:duration=0.4',
        '-vf','setpts=PTS+5/TB','-fps_mode','passthrough','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',
        '-video_track_timescale','1000',str(path)],timeout=20)
    service,result,generator=run(continuous,path,'video',tools=tools)
    sequence=result.sequence
    assert sequence.source_origin.fraction()==5
    assert sequence.selected_video_stream==0 and sequence.coverage=='complete_selected_video_stream'
    assert [f.source_pts for f in sequence.frames]==[5000,5200]
    assert [f.normalized_time.fraction() for f in sequence.frames]==[0,Fraction(1,5)]
    assert sequence.duration.fraction()==Fraction(2,5) and sequence.model_frame_sources==(0,1)


@pytest.mark.parametrize('pixel_format,color_args',[
    ('yuv420p10le',[]),
    ('yuv420p',['-color_trc','smpte2084','-color_primaries','bt2020','-colorspace','bt2020nc']),
])
def test_video_high_depth_or_hdr_waits_for_explicit_color_policy(continuous,tools,pixel_format,color_args):
    _,source,_=continuous;path=source/'explicit-color.mp4'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=size=32x32:rate=5:duration=0.4',
        '-c:v','libx264','-threads','1','-pix_fmt',pixel_format,*color_args,
        '-video_track_timescale','1000',str(path)],timeout=20)
    actual=tools.probe_run(path,timeout=10,extra=['-show_streams'])['streams'][0]
    assert actual['pix_fmt']==pixel_format
    generator=MockVideo()
    with pytest.raises(Pending,match='explicit_video_color_conversion_policy_required'):
        run(continuous,path,'video',tools=tools,generator=generator)
    assert not generator.calls


def test_time_grid_over_budget_never_drops_frames():
    with pytest.raises(Pending,match='requires_segmentation'):
        grid([Fraction(1,1000),Fraction(1,3)],64)


@pytest.mark.parametrize('change', ['input_sha256','model_revision','prompt_version'])
def test_generator_binding_mismatch(continuous,change):
    sought,source,workspace=continuous;path=source/'a.gif';gif(path)
    generator=MockVideo();generator.change[change]='b'*64
    with pytest.raises(ValueError,match='generation_binding'):run(continuous,path,'animation',generator=generator)


def test_static_rejected_before_model(continuous):
    sought,source,workspace=continuous;path=source/'static.png';Image.new('RGB',(32,32),'red').save(path)
    generator=MockVideo()
    with pytest.raises(Pending,match='continuous_source_required'):run(continuous,path,'animation',generator=generator)
    assert not generator.calls


def test_native_default_off(tmp_path):
    generator=LocalQwenVideoGenerator(tmp_path)
    assert not generator.readiness()['model_executed']
    with pytest.raises(RuntimeError,match='not_enabled'):
        generator(frame_bytes=(),fps=Rational.of(1),duration=Rational.of(1),input_sha256='a'*64,field_prompts=(),options=Options())
    with pytest.raises(ValueError,match='explicit_boolean'):LocalQwenVideoGenerator(tmp_path,enabled='false')


def test_duration_budget_leaves_whole_source_pending(continuous):
    sought,source,workspace=continuous;path=source/'long.gif'
    Image.new('RGBA',(32,32),'red').save(path,save_all=True,append_images=[Image.new('RGBA',(32,32),'blue')],
        duration=[2000,2000],loop=0)
    generator=MockVideo()
    with pytest.raises(Pending,match='duration_requires_segmentation'):run(continuous,path,'animation',generator=generator)
    assert not generator.calls


def test_webp_delays_read_after_decode(continuous):
    sought,source,workspace=continuous;path=source/'a.webp'
    Image.new('RGBA',(32,32),'red').save(path,save_all=True,append_images=[Image.new('RGBA',(32,32),'blue')],
        duration=[80,120],loop=3,lossless=True)
    _,result,_=run(continuous,path,'animation')
    assert [f.duration.fraction() for f in result.sequence.frames]==[Fraction(2,25),Fraction(3,25)]
    assert result.sequence.animation_plays==3


def test_no_frame_can_disappear_from_mapping(continuous):
    sought,source,workspace=continuous;path=source/'a.gif';gif(path)
    _,result,_=run(continuous,path,'animation')
    payload=result.sequence.model_dump(mode='json');payload['model_frame_sources']=[0]*5
    with pytest.raises(ValueError,match='complete_continuous_frame_mapping'):
        Sequence.model_validate_json(json.dumps(payload))


def test_transparent_hidden_rgb_never_becomes_visible(continuous):
    sought,source,workspace=continuous;path=source/'alpha.png'
    from media_tag.image_processing.animation import encode,Decoded
    frames=tuple(Image.new('RGBA',(32,32),c) for c in ((255,0,0,0),(0,0,255,255)))
    decoded=Decoded(frames,(Fraction(1,10),Fraction(1,10)),('animation','animation'),0,False,'PNG',None)
    path.write_bytes(encode(frames,decoded,'apng',1024**2))
    _,result,generator=run(continuous,path,'animation')
    with Image.open(BytesIO(generator.calls[0]['frame_bytes'][0])) as image:
        assert image.getpixel((64,64))==(255,255,255)
    assert result.sequence.frames[0].display_rgba_sha256==sha256(frames[0].tobytes()).hexdigest()


def test_hash_escape_and_source_overlap_reject_before_model(continuous):
    sought,source,workspace=continuous;path=source/'a.gif';gif(path)
    generator=MockVideo();service=TemporalTopicService(workspace,policy=sought.reviews.policy,generator=generator)
    args=dict(expected_source_sha256='b'*64,asset_id=sought.asset.asset_id,asset_revision=sought.asset.revision,
              analysis_id='test',content_kind='animation')
    with pytest.raises(ValueError,match='source_hash'):
        service.run(FilesystemBackend(source),path.name,**args)
    with pytest.raises(Exception):service.run(FilesystemBackend(source),'../outside.gif',**args)
    original={p.name:p.read_bytes() for p in source.iterdir()}
    service=TemporalTopicService(source,policy=sought.reviews.policy,generator=generator)
    with pytest.raises(ValueError,match='disjoint'):service.run(FilesystemBackend(source),path.name,**args)
    assert {p.name:p.read_bytes() for p in source.iterdir()}==original and not generator.calls


def test_video_audio_stream_is_not_analyzed(continuous,tools):
    sought,source,workspace=continuous;path=source/'with-audio.mp4'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','color=blue:size=32x32:rate=5:duration=0.4',
        '-f','lavfi','-i','sine=frequency=440:duration=0.8','-map','0:v','-map','1:a',
        '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-c:a','aac',str(path)],timeout=20)
    _,result,generator=run(continuous,path,'video',tools=tools)
    assert result.sequence.audio_policy=='not_analyzed' and result.sequence.duration.fraction()==Fraction(2,5)
    assert set(generator.calls[0])=={'frame_bytes','fps','duration','input_sha256','field_prompts','options'}


@pytest.mark.parametrize('change', ['hash','source','interval'])
def test_strict_result_and_sequence_binding(continuous,change):
    sought,source,workspace=continuous;path=source/'a.gif';gif(path)
    _,result,_=run(continuous,path,'animation')
    payload=result.model_dump(mode='json')
    if change=='hash':payload['sequence']['input_sha256']='b'*64
    elif change=='source':payload['sequence']['source_sha256']='b'*64
    else:payload['document']['evidence'][0]['end_seconds']=.3
    with pytest.raises(ValueError):Result.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize('budget,code', [('frames','source_frames_require_segmentation'),
                                       ('pixels','source_spatial_policy_required')])
def test_animation_decode_budget_becomes_explicit_pending(continuous,budget,code):
    sought,source,workspace=continuous;path=source/'budget.gif'
    images=[Image.new('RGB',(32,32),c) for c in ('red','blue','green','yellow')]
    images[0].save(path,save_all=True,append_images=images[1:],duration=[100]*4,loop=0)
    options=Options(max_source_frames=2) if budget=='frames' else Options(max_source_pixels=1)
    generator=MockVideo()
    with pytest.raises(Pending,match=code):run(continuous,path,'animation',options=options,generator=generator)
    assert not generator.calls


def test_native_label_cannot_advertise_unselected_model(continuous):
    sought,source,workspace=continuous;path=source/'a.gif';gif(path)
    generator=MockVideo();generator.change.update(backend='local_qwen_video',processor_identity='mock')
    with pytest.raises(ValueError,match='selected_local_model_binding'):
        run(continuous,path,'animation',generator=generator)
