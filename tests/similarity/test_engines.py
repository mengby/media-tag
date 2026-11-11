from hashlib import sha256
from io import BytesIO
import math
import json
import struct
import wave
import zlib

from PIL import Image
import pytest

from media_tag.similarity.contracts import VISUAL_VERSION, AudioFingerprint, Candidate, SourceVersion, VisualFingerprint
from media_tag.similarity.engines import SimilarityError, audio_bytes, compare, ranked, ranking_report, verified_bytes, video_bytes, visual_bytes

from .conftest import picture, reference


def sound(frequency=440.0, gain=.5, seconds=2.0, rate=16000, silence=False):
    stream=BytesIO()
    with wave.open(stream,'wb') as output:
        output.setnchannels(1);output.setsampwidth(2);output.setframerate(rate)
        output.writeframes(b''.join(struct.pack('<h',0 if silence else int(32760*gain*math.sin(2*math.pi*frequency*i/rate))) for i in range(round(rate*seconds))))
    return stream.getvalue()


def test_visual_ranking_survives_resize_and_keeps_exact_byte_identity_separate():
    original=picture()
    resized=picture(size=96)
    different=picture(shape='square',color='blue')
    query=visual_bytes(original,reference(original,'left'))
    near=visual_bytes(resized,reference(resized,'near'))
    far=visual_bytes(different,reference(different,'far'))
    same=visual_bytes(original,reference(original,'same'))
    candidates=ranked(query,[far,near,same])
    assert candidates[0].comparison=='exact_bytes' and candidates[0].right.asset_id=='same'
    assert candidates[1].right.asset_id=='near'
    assert compare(query,near).score > compare(query,far).score
    assert all(not value.calibrated and not value.duplicate_confirmed and not value.labels_copied for value in candidates)
    assert query.source.content_sha256==sha256(original).hexdigest()


def test_transparency_hides_invisible_rgb_but_visible_silhouettes_still_differ():
    def transparent(rgb):
        out=BytesIO();Image.new('RGBA',(8,8),(*rgb,0)).save(out,'PNG');return out.getvalue()
    left,right=transparent((255,0,0)),transparent((0,0,255))
    a=visual_bytes(left,reference(left,'a'));b=visual_bytes(right,reference(right,'b'))
    assert a.frames[0].dhash==b.frames[0].dhash and a.frames[0].mean_rgb==b.frames[0].mean_rgb
    assert compare(a,b).comparison=='visual_overlap'
    visible=picture(alpha=True)
    c=visual_bytes(visible,reference(visible,'visible'))
    assert a.frames[0].alpha_hash!=c.frames[0].alpha_hash


def test_animation_processes_all_frames_and_records_timing_without_motion_claim():
    frames=[Image.new('RGBA',(8,8),color) for color in ('red','blue')]
    output=BytesIO();frames[0].save(output,'PNG',save_all=True,append_images=frames[1:],duration=[100,300],loop=0)
    data=output.getvalue()
    descriptor=visual_bytes(data,reference(data))
    assert descriptor.media_kind=='animation' and descriptor.coverage=='complete_animation_frames'
    assert len(descriptor.frames)==2 and descriptor.frame_durations_ms==(100,300)
    assert descriptor.frames[1].normalized_seconds==.1 and not descriptor.full_video_observed
    with pytest.raises(SimilarityError,match='animation_frame_budget'):
        visual_bytes(data,reference(data),max_frames=1)


def test_hash_profile_and_budget_failures_are_explicit(tmp_path):
    data=picture();path=tmp_path/'source.png';path.write_bytes(data)
    with pytest.raises(SimilarityError,match='source_integrity'):
        verified_bytes(path,'0'*64)
    with pytest.raises(SimilarityError,match='source_byte_budget'):
        verified_bytes(path,sha256(data).hexdigest(),max_bytes=1)
    with pytest.raises(SimilarityError,match='visual_pixel_budget'):
        visual_bytes(data,reference(data),max_pixels=16)
    with pytest.raises(ValueError):
        Candidate(left=reference(data),right=reference(data,'b'),left_descriptor_sha256='a'*64,
            right_descriptor_sha256='b'*64,algorithm_version=VISUAL_VERSION,comparison='visual_overlap',score=.9,duplicate_confirmed=True)


def test_audio_uses_decoded_sound_and_gain_invariance_not_filenames(tmp_path, tools):
    pytest.importorskip('numpy')
    a,b,c=sound(),sound(gain=.2),sound(frequency=880)
    left=audio_bytes(a,reference(a,'tone-a'),tmp_path/'a',tools)
    near=audio_bytes(b,reference(b,'tone-b'),tmp_path/'b',tools)
    far=audio_bytes(c,reference(c,'tone-c'),tmp_path/'c',tools)
    assert left.coverage=='complete_selected_audio_stream' and left.decoded_duration_seconds==pytest.approx(2.0)
    assert len(left.spectral_frames[0])==24 and left.source.content_sha256!=near.source.content_sha256
    assert compare(left,near).score > compare(left,far).score
    assert ranked(left,[far,near])[0].right.asset_id=='tone-b'


def test_audio_reencoding_is_compared_from_real_pcm_and_silence_is_not_unique(tmp_path, tools):
    pytest.importorskip('numpy')
    data=sound(seconds=1.0)
    original=tmp_path/'original.wav';original.write_bytes(data)
    encoded=tmp_path/'encoded.flac'
    tools.ffmpeg_run(['-v','error','-i',str(original),'-c:a','flac',str(encoded)],timeout=10.0)
    left=audio_bytes(data,reference(data,'wav'),tmp_path/'wav',tools)
    right_data=encoded.read_bytes();right=audio_bytes(right_data,reference(right_data,'flac'),tmp_path/'flac',tools)
    assert compare(left,right).comparison=='audio_overlap' and compare(left,right).score==pytest.approx(1.0,abs=.001)
    silent=sound(silence=True);other=sound(silence=True,seconds=1.1)
    s1=audio_bytes(silent,reference(silent,'silence-a'),tmp_path/'s1',tools)
    s2=audio_bytes(other,reference(other,'silence-b'),tmp_path/'s2',tools)
    with pytest.raises(SimilarityError,match='insufficient_audio_signal'):
        compare(s1,s2)
    assert ranked(s1,[s2])==[]
    report=ranking_report(s1,[s2])
    assert report['state']=='insufficient_information' and report['skipped'][0]['reason']=='insufficient_audio_signal'


def test_audio_duration_budget_does_not_silently_sample_longer_sound(tmp_path,tools):
    pytest.importorskip('numpy')
    data=sound(seconds=2.0)
    with pytest.raises(SimilarityError,match='audio_duration_budget'):
        audio_bytes(data,reference(data),tmp_path/'limited',tools,max_seconds=.5)


def test_video_fingerprint_uses_actual_pts_samples_and_preserves_source(tmp_path,tools):
    source=tmp_path/'synthetic.mp4'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=s=32x24:r=5','-t','1.0','-an','-c:v','libx264',str(source)],timeout=10.0)
    data=source.read_bytes();before=sha256(data).hexdigest()
    result=video_bytes(data,reference(data,'video'),tmp_path/'decoded',tools,max_frames=3)
    assert result.coverage=='sparse_decoded_video_frames' and not result.full_video_observed
    assert len(result.frames)==3 and all(f.normalized_seconds is not None for f in result.frames)
    assert result.frames[0].normalized_seconds==0.0 and result.frames[-1].normalized_seconds>0
    assert sha256(source.read_bytes()).hexdigest()==before


def test_multiple_audio_streams_require_explicit_selection(tmp_path,tools):
    pytest.importorskip('numpy')
    path=tmp_path/'multiple.mka'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=16000',
        '-f','lavfi','-i','sine=frequency=880:sample_rate=16000','-t','1.0','-map','0:a','-map','1:a',
        '-c:a','pcm_s16le',str(path)],timeout=10.0)
    data=path.read_bytes()
    with pytest.raises(SimilarityError,match='audio_track_selection_required'):
        audio_bytes(data,reference(data),tmp_path/'unselected',tools)
    result=audio_bytes(data,reference(data),tmp_path/'selected',tools,stream_index=1)
    assert result.source_stream_index==1 and result.decoded_duration_seconds==pytest.approx(1.0)


def test_video_nonzero_pts_are_normalized_from_the_actual_stream_origin(tmp_path,tools):
    source=tmp_path/'plain.mp4';offset=tmp_path/'offset.mp4'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=s=32x24:r=5','-t','1.0','-an','-c:v','libx264',str(source)],timeout=10.0)
    tools.ffmpeg_run(['-v','error','-i',str(source),'-c','copy','-output_ts_offset','5',str(offset)],timeout=10.0)
    data=offset.read_bytes()
    descriptor=video_bytes(data,reference(data,'offset'),tmp_path/'offset-decoded',tools,max_frames=3)
    assert descriptor.source_origin_seconds==pytest.approx(5.0)
    assert descriptor.frames[0].normalized_seconds==0.0 and descriptor.frames[-1].normalized_seconds==pytest.approx(.8)


def test_missing_audio_dependency_is_explicit_and_does_not_create_fake_result(tmp_path,tools,monkeypatch):
    import builtins
    original=builtins.__import__
    def unavailable(name,*args,**kwargs):
        if name=='numpy':
            raise ImportError('synthetic unavailable optional extra')
        return original(name,*args,**kwargs)
    monkeypatch.setattr(builtins,'__import__',unavailable)
    data=sound(seconds=.1)
    work=tmp_path/'not-created'
    with pytest.raises(SimilarityError,match='audio_numpy_extra_unavailable'):
        audio_bytes(data,reference(data),work,tools)
    assert not work.exists()


def test_delayed_selected_audio_decodes_to_eof_without_container_timeline_truncation(tmp_path,tools):
    pytest.importorskip('numpy')
    path=tmp_path/'delayed-audio.mkv'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','color=c=black:s=32x24:r=5:d=4',
        '-itsoffset','2','-f','lavfi','-i','sine=frequency=440:sample_rate=16000:duration=2',
        '-map','0:v','-map','1:a','-c:v','libx264','-c:a','pcm_s16le','-t','4',str(path)],timeout=10.0)
    data=path.read_bytes()
    descriptor=audio_bytes(data,reference(data,'delayed-audio'),tmp_path/'delayed-decoded',tools,stream_index=1,max_seconds=3.0)
    assert descriptor.source_origin_seconds==pytest.approx(2.0)
    assert descriptor.decoded_duration_seconds==pytest.approx(2.0,abs=1/16000.0)
    assert descriptor.decoded_sample_count==32000
    assert descriptor.decoded_clock=='pcm_samples_from_selected_stream_start'
    assert descriptor.pts_normalization=='asetpts=PTS-STARTPTS'
    assert descriptor.algorithm_version=='ct-audio-logbands-v2'


def test_audio_without_stream_duration_rejects_output_budget_instead_of_returning_prefix(tmp_path,tools):
    pytest.importorskip('numpy')
    path=tmp_path/'long-delayed-audio.mkv'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','color=c=black:s=32x24:r=5:d=8',
        '-itsoffset','2','-f','lavfi','-i','sine=frequency=440:sample_rate=16000:duration=6',
        '-map','0:v','-map','1:a','-c:v','libx264','-c:a','pcm_s16le','-t','8',str(path)],timeout=10.0)
    audio=next(stream for stream in tools.probe_run(path,timeout=10.0)['streams'] if stream['codec_type']=='audio')
    assert audio.get('duration') is None
    data=path.read_bytes()
    with pytest.raises(SimilarityError,match='audio_output_budget'):
        audio_bytes(data,reference(data,'long-delayed-audio'),tmp_path/'long-delayed-decoded',tools,stream_index=1,max_seconds=3.0)


def test_webp_frame_timing_is_read_after_decode_instead_of_becoming_unknown():
    images=[Image.new('RGBA',(8,8),color) for color in ('red','blue')]
    stream=BytesIO();images[0].save(stream,'WEBP',save_all=True,append_images=images[1:],duration=[80,120],loop=0,lossless=True)
    data=stream.getvalue();descriptor=visual_bytes(data,reference(data,'webp-animation'))
    assert descriptor.frame_durations_ms==(80.0,120.0)
    assert descriptor.frames[1].normalized_seconds==pytest.approx(.08,abs=1e-12)
    assert descriptor.algorithm_version=='ct-visual-dhash-alpha-v2'


def fractional_apng():
    images=[Image.new('RGBA',(8,8),color) for color in ('red','blue')]
    stream=BytesIO();images[0].save(stream,'PNG',save_all=True,append_images=images[1:],duration=[17,8],loop=0)
    data=stream.getvalue();parts=[data[:8]];offset=8;frame=0
    while offset < len(data):
        size=struct.unpack('>I',data[offset:offset+4])[0]
        kind=data[offset+4:offset+8];payload=data[offset+8:offset+8+size]
        if kind==b'fcTL':
            payload=bytearray(payload)
            struct.pack_into('>HH',payload,20,1,60 if frame==0 else 120)
            payload=bytes(payload);frame+=1
        parts.append(struct.pack('>I',len(payload))+kind+payload+struct.pack('>I',zlib.crc32(kind+payload)))
        offset+=12+size
    assert frame==2
    return b''.join(parts)


def test_fractional_apng_delays_do_not_accumulate_integer_truncation():
    data=fractional_apng();descriptor=visual_bytes(data,reference(data,'fractional-apng'))
    assert descriptor.frame_durations_ms==pytest.approx((1000.0/60,1000.0/120),abs=1e-12)
    assert descriptor.frames[1].normalized_seconds==pytest.approx(1.0/60,abs=1e-12)
    assert descriptor.frames[1].normalized_seconds!=.016


def test_legacy_visual_and_audio_v1_descriptors_cannot_parse_as_current(tmp_path,tools):
    data=picture();visual=visual_bytes(data,reference(data,'visual'))
    legacy=visual.model_dump(mode='json')
    legacy.update(schema_version='ct-visual-fingerprint-v1',algorithm_version='ct-visual-dhash-alpha-v1')
    with pytest.raises(ValueError):
        VisualFingerprint.model_validate_json(json.dumps(legacy))
    with pytest.raises(SimilarityError,match='incompatible_descriptor_profiles'):
        compare(visual.model_copy(update={'algorithm_version':'ct-visual-dhash-alpha-v1'}),
            visual.model_copy(update={'algorithm_version':'ct-visual-dhash-alpha-v1'}))
    pytest.importorskip('numpy')
    audio_data=sound(seconds=.5)
    audio=audio_bytes(audio_data,reference(audio_data,'audio'),tmp_path/'current-audio',tools)
    invalid=audio.model_dump(mode='json');invalid['decoded_sample_count']=1
    with pytest.raises(ValueError,match='actual PCM sample count'):
        AudioFingerprint.model_validate_json(json.dumps(invalid))
    legacy=audio.model_dump(mode='json')
    legacy.update(schema_version='ct-audio-fingerprint-v1',algorithm_version='ct-audio-logbands-v1')
    for key in ('decoded_sample_count','decoded_clock','pts_normalization'):
        legacy.pop(key)
    with pytest.raises(ValueError):
        AudioFingerprint.model_validate_json(json.dumps(legacy))
