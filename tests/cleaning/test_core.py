import struct
import wave
import json
import re

from PIL import Image
import pytest

from media_tag.cleaning import CleaningError, CleaningRecipe, VideoSegment
from media_tag.storage import StorageError
from media_tag.workers.probe import file_sha256


def output(clean, result):
    return clean.service.workspace/result['output_ref']


def test_static_alpha_copy_naming_mapping_and_cross_job_hash_reuse(clean):
    source = clean.originals/'misleading-video.mp4'
    Image.new('RGBA', (9, 7), (240, 20, 0, 80)).save(source, format='PNG')
    before = source.read_bytes()
    recipe = clean.recipe(source, 'image')
    first = clean.service.run(source, recipe, job_id='image-first')
    second = clean.service.run(source, recipe, job_id='image-second')
    assert source.read_bytes() == before == output(clean, first).read_bytes()
    assert first['standardized_name'].endswith('.png')
    assert first['standardized_name'] == second['standardized_name'] and second['cache_reused']
    assert first['name_mapping']['original_name'] == source.name
    assert first['source_quality']['frames'][0]['has_transparent_pixels']
    assert not first['privacy_confirmation'] and not first['quality_confirmation']
    assert not first['semantic_labels_generated']


def test_gif_loop_durations_and_alpha_preserved_without_video_duration_rule(clean):
    source = clean.originals/'synthetic.gif'
    frames = [Image.new('RGBA', (8, 8), color) for color in ((255, 0, 0, 0), (0, 0, 255, 255))]
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100, 300], loop=2, transparency=0)
    recipe = clean.recipe(source, 'animation')
    result = clean.service.run(source, recipe, job_id='animation')
    assert output(clean, result).read_bytes() == source.read_bytes()
    assert result['source_quality']['frame_count'] == 2
    assert [f['duration_ms'] for f in result['source_quality']['frames']] == [100, 300]
    assert result['source_quality']['loop'] == 2
    assert result['duration_target_state'] == 'not_a_video_duration_rule'
    with pytest.raises(ValueError):
        CleaningRecipe(source_sha256=recipe.source_sha256, content_kind='animation', speed=2.0)


def test_bgm_and_sfx_audio_copy_keeps_complete_original_duration(clean):
    source = clean.originals/'synthetic.wav'
    with wave.open(str(source), 'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(8000); out.writeframes(b'\0\0'*16000)
    result = clean.service.run(source, clean.recipe(source, 'audio'), job_id='audio')
    assert output(clean, result).read_bytes() == source.read_bytes()
    assert result['source_quality']['full_decode'] and result['source_quality']['track_count'] == 1
    assert result['duration_target_state'] == 'not_a_video_duration_rule'


def test_physical_clip_excludes_red_prefix_and_uses_real_blue_frames(clean):
    source = clean.video()
    digest = file_sha256(source)
    result = clean.service.run(source, clean.recipe(source, 'video', segment=VideoSegment(start_seconds=4.0, end_seconds=8.0)), job_id='blue-clip')
    assert result['source_quality']['decoded_frame_count'] == 40
    assert result['source_quality']['actual_output_duration_seconds'] == pytest.approx(4.0, abs=.0001)
    assert result['time_map']['source_start_seconds'] == 4.0
    assert result['time_map']['frames'][0]['index'] == 40
    assert not result['segments_are_annotations_only'] and result['duration_target_state'] == 'in_3_to_5'
    raw, _ = clean.tools.ffmpeg_run(['-v', 'error', '-i', str(output(clean, result)), '-frames:v', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'], timeout=10.0)
    assert sum(raw[2::3])/len(raw[2::3]) > 220 and sum(raw[0::3])/len(raw[0::3]) < 25
    assert file_sha256(source) == digest


def test_explicit_segment_target_retimes_to_three_seconds_without_stacking(clean):
    source = clean.video()
    recipe = clean.recipe(source, 'video', segment=VideoSegment(start_seconds=0.0, end_seconds=6.0), target_duration_seconds=3.0)
    first = clean.service.run(source, recipe, job_id='target-three')
    second = clean.service.run(source, recipe, job_id='target-three-again')
    assert first['time_map']['speed'] == second['time_map']['speed'] == 2.0
    assert first['time_map']['output_duration_seconds'] == 3.0
    assert second['cache_reused'] and first['output_sha256'] == second['output_sha256']
    assert first['source_quality']['decoded_frame_count'] == 60
    with pytest.raises(ValueError):
        clean.recipe(source, 'video', target_duration_seconds=3.0)
    with pytest.raises(CleaningError, match='new source time base'):
        clean.service.run(output(clean, first), clean.recipe(output(clean, first), 'video'), job_id='stacked')


def test_nonzero_source_origin_uses_stream_span_and_maps_actual_segment(clean):
    source = clean.video()
    offset = clean.originals/'offset.mp4'
    clean.tools.ffmpeg_run(['-v', 'error', '-i', str(source), '-c', 'copy', '-output_ts_offset', '5', str(offset)], timeout=20.0)
    result = clean.service.run(offset, clean.recipe(offset, 'video', segment=VideoSegment(start_seconds=2.0, end_seconds=6.0)), job_id='offset')
    clock = result['time_map']
    assert clock['source_origin_seconds'] == pytest.approx(5.0)
    assert clock['source_stream_duration_seconds'] == pytest.approx(8.0)
    assert clock['absolute_start_seconds'] == pytest.approx(7.0)
    assert result['source_quality']['actual_output_start_seconds'] == pytest.approx(0.0, abs=.0001)
    assert result['source_quality']['actual_output_duration_seconds'] == pytest.approx(4.0, abs=.0001)


def test_audio_policy_is_explicit_and_actual_preserve_pitch_and_remove_outputs_are_checked(clean):
    source = clean.video(audio=True)
    segment = VideoSegment(start_seconds=0.0, end_seconds=6.0)
    with pytest.raises(ValueError, match='choose preserve_pitch or remove'):
        clean.service.run(source, clean.recipe(source, 'video', segment=segment), job_id='unselected-audio')
    result = clean.service.run(source, clean.recipe(source, 'video', segment=segment, speed=2.0, audio_policy='preserve_pitch'), job_id='with-audio')
    assert len(result['source_quality']['audio_checks']) == 1
    raw, _ = clean.tools.ffmpeg_run(['-v', 'error', '-i', str(output(clean, result)), '-map', '0:a:0', '-ss', '0.5', '-t', '1', '-ac', '1', '-ar', '48000', '-f', 's16le', '-'], timeout=10.0)
    samples = struct.unpack('<'+'h'*(len(raw)//2), raw)
    crossings = sum(a <= 0 < b for a, b in zip(samples, samples[1:]))
    assert 430 <= crossings <= 450
    silent = clean.service.run(source, clean.recipe(source, 'video', segment=segment, audio_policy='remove'), job_id='without-audio')
    assert not silent['source_quality']['audio_checks']
    info = clean.tools.probe_run(output(clean, silent), timeout=10.0)
    assert not any(s['codec_type'] == 'audio' for s in info['streams'])


def test_multiple_audio_tracks_require_explicit_selection(clean):
    source = clean.video(audio=True, second_audio=True)
    segment = VideoSegment(start_seconds=0.0, end_seconds=4.0)
    with pytest.raises(ValueError, match='multiple audio tracks'):
        clean.service.run(source, clean.recipe(source, 'video', segment=segment, audio_policy='preserve_pitch'), job_id='audio-ambiguous')
    result = clean.service.run(source, clean.recipe(source, 'video', segment=segment,
        audio_policy='preserve_pitch', audio_stream_index=2), job_id='second-audio')
    assert result['time_map']['audio']['source_stream_index'] == 2
    assert len(result['source_quality']['audio_checks']) == 1


def test_alignment_is_explicit_and_output_is_not_silently_padded_to_duration_rule(clean):
    source = clean.video()
    with pytest.raises(ValueError, match='source frame boundaries'):
        clean.service.run(source, clean.recipe(source, 'video', segment=VideoSegment(start_seconds=1.15, end_seconds=4.15)), job_id='unaligned')
    result = clean.service.run(source, clean.recipe(source, 'video', segment=VideoSegment(start_seconds=1.15, end_seconds=4.15, alignment='inside')), job_id='inside')
    assert result['time_map']['source_start_seconds'] == pytest.approx(1.2)
    assert result['time_map']['source_end_seconds'] == pytest.approx(4.1)
    assert result['time_map']['output_duration_seconds'] == pytest.approx(2.9)
    assert result['duration_target_state'] == 'requires_duration_decision'


def test_cache_integrity_and_job_request_identity_are_verified(clean):
    source = clean.originals/'synthetic.png'
    Image.new('RGB', (8, 8), 'red').save(source)
    recipe = clean.recipe(source, 'image')
    first = clean.service.run(source, recipe, job_id='initial')
    bundle = clean.service.cache/first['cache_key'][:24]
    (bundle/first['artifact_name']).write_bytes(b'synthetic corruption')
    repaired = clean.service.run(source, recipe, job_id='repaired')
    assert repaired['invalid_cache_rebuilt'] and repaired['output_sha256'] == first['output_sha256']
    assert file_sha256(source) == recipe.source_sha256
    other = clean.originals/'other-name.png'; other.write_bytes(source.read_bytes())
    with pytest.raises(CleaningError, match='job ID reused'):
        clean.service.run(other, recipe, job_id='initial')


def test_source_hash_and_actual_kind_cannot_be_faked(clean):
    source = clean.originals/'synthetic.png'
    Image.new('RGB', (8, 8), 'red').save(source)
    with pytest.raises(CleaningError, match='source hash'):
        clean.service.run(source, CleaningRecipe(source_sha256='0'*64, content_kind='image'), job_id='wrong-sha')
    with pytest.raises(CleaningError, match='actual source kind'):
        clean.service.run(source, clean.recipe(source, 'audio'), job_id='wrong-kind')


def test_whole_video_default_stays_one_x_and_requires_duration_decision(clean):
    source = clean.video()
    result = clean.service.run(source, clean.recipe(source, 'video'), job_id='whole-unselected')
    assert result['time_map']['speed'] == 1.0
    assert result['source_quality']['actual_output_duration_seconds'] == pytest.approx(8.0)
    assert result['duration_target_state'] == 'requires_duration_decision'


def test_audio_offset_is_preserved_for_explicit_intersection_and_not_silently_trimmed(clean):
    source = clean.video(audio=True)
    target = clean.originals/'offset-audio.mp4'
    clean.tools.ffmpeg_run(['-v', 'error', '-i', str(source), '-itsoffset', '2', '-i', str(source),
        '-map', '0:v', '-map', '1:a', '-c', 'copy', str(target)], timeout=20.0)
    with pytest.raises(ValueError, match='audio extends outside video'):
        clean.service.run(target, clean.recipe(target, 'video', audio_policy='preserve_pitch'), job_id='no-implicit-audio-trim')
    result = clean.service.run(target, clean.recipe(target, 'video', audio_policy='preserve_pitch',
        segment=VideoSegment(start_seconds=0.0, end_seconds=4.0)), job_id='audio-offset')
    audio = result['time_map']['audio']
    assert audio['output_start_seconds'] == pytest.approx(2.0, abs=.03)
    assert audio['output_duration_seconds'] == pytest.approx(2.0, abs=.03)
    actual = result['source_quality']['audio_checks'][0]
    assert actual['actual_start_seconds'] == pytest.approx(audio['output_start_seconds'], abs=actual['tolerance_seconds'])


def test_decoders_use_private_snapshot_and_source_changes_prevent_publication(clean, monkeypatch):
    import media_tag.cleaning.service as module
    source = clean.video()
    recipe = clean.recipe(source, 'video', segment=VideoSegment(start_seconds=0.0, end_seconds=4.0))
    original_render = module.render_video
    paths = []
    def changed(path, *args, **kwargs):
        paths.append(path)
        assert path != source and file_sha256(path) == recipe.source_sha256
        source.write_bytes(b'synthetic outside writer changed the source')
        return original_render(path, *args, **kwargs)
    monkeypatch.setattr(module, 'render_video', changed)
    with pytest.raises(CleaningError, match='source hash'):
        clean.service.run(source, recipe, job_id='changed-source')
    assert paths and not (clean.service.workspace/'jobs'/'changed-source'/'cleaning'/'result.json').exists()


def test_output_byte_budget_is_not_bypassed_by_encoded_copy(clean):
    from media_tag.cleaning import CleaningService, CleaningLimits
    source = clean.originals/'budget.png'
    Image.new('RGBA', (16, 16), (20, 30, 40, 80)).save(source)
    service = CleaningService(clean.service.workspace/'limited', clean.tools, limits=CleaningLimits(max_output_bytes=10))
    with pytest.raises(CleaningError, match='copy byte budget'):
        service.run(source, clean.recipe(source, 'image'), job_id='small-budget')


def test_missing_audio_policy_is_recorded_as_waiting_instead_of_execution_failure(clean):
    from media_tag.cleaning import CleaningPending
    source = clean.video(audio=True)
    with pytest.raises(CleaningPending) as error:
        clean.service.run(source, clean.recipe(source, 'video'), job_id='policy-pending')
    assert error.value.code == 'audio_policy_required' and error.value.state == 'waiting_policy'
    job = clean.service.workspace/'jobs'/'policy-pending'/'cleaning'
    state = json.loads((job/'status.json').read_text(encoding='utf8'))
    assert state['state'] == 'waiting_policy' and state['code'] == 'audio_policy_required'
    assert not (job/'failure.json').exists() and not (job/'result.json').exists()


def test_non_square_pixels_wait_for_display_ratio_policy_without_encoding_warped_copy(clean):
    from media_tag.cleaning import CleaningPending
    source = clean.originals/'anamorphic.mp4'
    clean.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'color=c=red:s=64x32:r=5:d=4',
        '-vf', 'setsar=2/1', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(source)], timeout=20.0)
    info = clean.tools.probe_run(source, timeout=10.0)
    stream = next(s for s in info['streams'] if s['codec_type'] == 'video')
    assert stream['sample_aspect_ratio'] == '2:1'
    before = file_sha256(source)
    with pytest.raises(CleaningPending) as pending:
        clean.service.run(source, clean.recipe(source, 'video'), job_id='anamorphic')
    assert pending.value.code == 'sample_aspect_ratio_policy_required'
    job = clean.service.workspace/'jobs'/'anamorphic'/'cleaning'
    state = json.loads((job/'status.json').read_text(encoding='utf8'))
    assert state['state'] == 'waiting_policy' and state['code'] == pending.value.code
    assert not (job/'result.json').exists()
    assert not list(clean.service.cache.glob('*/media.mp4'))
    assert file_sha256(source) == before


def test_actual_x264_encoder_uses_one_thread_not_just_one_decoder_thread(clean, monkeypatch):
    source = clean.originals/'encoder-budget.mp4'
    clean.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=s=640x360:r=5:d=4',
        '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(source)], timeout=20.0)
    native = clean.tools.ffmpeg_run
    logs = []
    def observed(args, **kwargs):
        encoding = '-c:v' in args and args[args.index('-c:v')+1] == 'libx264'
        changed = list(args)
        if encoding:
            changed[changed.index('-v')+1] = 'info'
        out, error = native(changed, **kwargs)
        if encoding:
            logs.append(error.decode('utf8', errors='replace'))
        return out, error
    monkeypatch.setattr(clean.tools, 'ffmpeg_run', observed)
    result = clean.service.run(source, clean.recipe(source, 'video'), job_id='one-encoder-thread')
    assert logs and re.findall(r'\bthreads=(\d+)', '\n'.join(logs)) == ['1']
    assert result['source_quality']['video_encoder_threads'] == 1
    assert result['source_quality']['actual_output_duration_seconds'] == pytest.approx(4.0, abs=.0001)


def _assert_failed_adoption(job):
    state = json.loads((job/'status.json').read_text(encoding='utf8'))
    failure = json.loads((job/'failure.json').read_text(encoding='utf8'))
    assert state['state'] == failure['state'] == 'failed'
    assert state['code'] == failure['code']
    assert not (job/'result.json').exists()
    assert list(job.glob('result.history-*.json'))


def test_source_change_after_cache_hit_invalidates_old_result_and_failure_is_recoverable(clean, monkeypatch):
    source = clean.originals/'cache-source.png'
    Image.new('RGB', (8, 8), 'red').save(source)
    original_bytes = source.read_bytes()
    recipe = clean.recipe(source, 'image')
    first = clean.service.run(source, recipe, job_id='cache-source-change')
    job = output(clean, first).parent
    cached = clean.service._cached
    def changed(*args, **kwargs):
        report = cached(*args, **kwargs)
        source.write_bytes(b'synthetic source changed after cache qualification')
        return report
    monkeypatch.setattr(clean.service, '_cached', changed)
    with pytest.raises(CleaningError, match='source hash'):
        clean.service.run(source, recipe, job_id='cache-source-change')
    _assert_failed_adoption(job)
    monkeypatch.setattr(clean.service, '_cached', cached)
    source.write_bytes(original_bytes)
    recovered = clean.service.run(source, recipe, job_id='cache-source-change')
    assert recovered['cache_reused'] and recovered['output_sha256'] == first['output_sha256']
    assert json.loads((job/'status.json').read_text(encoding='utf8'))['state'] == 'succeeded'
    assert (job/'result.json').exists() and not (job/'failure.json').exists()


@pytest.mark.parametrize('fault', ['copy', 'verification', 'result_publication'])
def test_job_copy_verification_and_result_write_failures_never_leave_checking_or_old_success(clean, monkeypatch, fault):
    import media_tag.cleaning.service as module
    source = clean.originals/'job-fault.png'
    Image.new('RGB', (8, 8), 'red').save(source)
    recipe = clean.recipe(source, 'image')
    first = clean.service.run(source, recipe, job_id='job-fault')
    target = output(clean, first)
    job = target.parent
    if fault == 'copy':
        copy = module._copy_bounded
        def failed_copy(original, destination, maximum):
            if str(destination.name) == str(target):
                raise OSError('synthetic job copy failure')
            return copy(original, destination, maximum)
        target.unlink()
        monkeypatch.setattr(module, '_copy_bounded', failed_copy)
    elif fault == 'verification':
        hashed = module.file_sha256
        monkeypatch.setattr(module, 'file_sha256', lambda path: '0'*64 if path == target else hashed(path))
    else:
        write = module._write_json
        def failed_result(path, value):
            if path == job/'result.json':
                raise OSError('synthetic result publication failure')
            return write(path, value)
        monkeypatch.setattr(module, '_write_json', failed_result)
    with pytest.raises((OSError, CleaningError, StorageError)):
        clean.service.run(source, recipe, job_id='job-fault')
    _assert_failed_adoption(job)
    assert file_sha256(source) == recipe.source_sha256
    monkeypatch.undo()
    recovered = clean.service.run(source, recipe, job_id='job-fault')
    assert recovered['cache_reused'] and recovered['output_sha256'] == first['output_sha256']
    assert json.loads((job/'status.json').read_text(encoding='utf8'))['state'] == 'succeeded'
    assert (job/'result.json').exists() and not (job/'failure.json').exists()


def test_waiting_during_adoption_retires_old_result_without_marking_execution_failure(clean, monkeypatch):
    from media_tag.cleaning import CleaningPending
    source = clean.originals/'waiting-adoption.png'
    Image.new('RGB', (8, 8), 'red').save(source)
    recipe = clean.recipe(source, 'image')
    first = clean.service.run(source, recipe, job_id='waiting-adoption')
    job = output(clean, first).parent
    def waiting(*args, **kwargs):
        raise CleaningPending('synthetic evidence condition changed during adoption',
            code='verification_evidence_required', state='waiting_evidence')
    monkeypatch.setattr(clean.service, '_adopt', waiting)
    with pytest.raises(CleaningPending):
        clean.service.run(source, recipe, job_id='waiting-adoption')
    status = json.loads((job/'status.json').read_text(encoding='utf8'))
    assert status['state'] == 'waiting_evidence' and status['code'] == 'verification_evidence_required'
    assert not (job/'result.json').exists() and not (job/'failure.json').exists()
    assert list(job.glob('result.history-*.json'))


@pytest.mark.parametrize('field,forged', [
    ('original_preserved', False), ('quality_confirmation', True), ('privacy_confirmation', True),
    ('medical_confirmation', True), ('license_status', 'verified'), ('semantic_labels_generated', True),
    ('face_redaction_applied', True), ('watermark_removal_applied', True)])
def test_cache_report_and_matching_sidecar_cannot_forge_approval_or_capabilities(clean, field, forged):
    source = clean.originals/'cache-approval.png'
    Image.new('RGBA', (8, 8), (40, 50, 60, 80)).save(source)
    recipe = clean.recipe(source, 'image')
    first = clean.service.run(source, recipe, job_id='approval-original')
    bundle = clean.service.cache/first['cache_key'][:24]
    path = bundle/'report.json'
    report = json.loads(path.read_text(encoding='utf8'))
    report[field] = forged
    path.write_text(json.dumps(report), encoding='utf8')
    (bundle/'report.sha256').write_text(file_sha256(path)+'\n', encoding='ascii')
    with pytest.raises(CleaningError) as rejected:
        clean.service._cached(bundle, first['cache_key'], recipe)
    assert rejected.value.code == 'cache_approval_or_capability_invalid'
    rebuilt = clean.service.run(source, recipe, job_id='approval-rebuilt')
    assert rebuilt['invalid_cache_rebuilt'] and not rebuilt['cache_reused']
    assert rebuilt['original_preserved'] is True and rebuilt['license_status'] == 'unverified'
    assert all(rebuilt[key] is False for key in ('quality_confirmation', 'privacy_confirmation',
        'medical_confirmation', 'semantic_labels_generated', 'face_redaction_applied', 'watermark_removal_applied'))
    assert rebuilt['output_sha256'] == first['output_sha256'] == file_sha256(source)


def test_final_result_reasserts_core_metadata_instead_of_trusting_cache_dictionary(clean, monkeypatch):
    source = clean.originals/'final-invariants.png'
    Image.new('RGB', (8, 8), 'red').save(source)
    recipe = clean.recipe(source, 'image')
    clean.service.run(source, recipe, job_id='final-first')
    cached = clean.service._cached
    def unsafe_dictionary(*args, **kwargs):
        report = cached(*args, **kwargs)
        return report | {'original_preserved': False, 'quality_confirmation': True,
            'privacy_confirmation': True, 'medical_confirmation': True, 'license_status': 'verified',
            'semantic_labels_generated': True, 'face_redaction_applied': True, 'watermark_removal_applied': True}
    monkeypatch.setattr(clean.service, '_cached', unsafe_dictionary)
    result = clean.service.run(source, recipe, job_id='final-second')
    assert result['original_preserved'] is True and result['license_status'] == 'unverified'
    assert all(result[key] is False for key in ('quality_confirmation', 'privacy_confirmation',
        'medical_confirmation', 'semantic_labels_generated', 'face_redaction_applied', 'watermark_removal_applied'))


@pytest.mark.parametrize('where', ['cache_root', 'ordinary_child', 'outside_child'])
def test_staging_cleanup_rejects_cache_root_and_non_generated_or_outside_directories(clean, where):
    if where == 'cache_root':
        target = clean.service.cache
    elif where == 'ordinary_child':
        target = clean.service.cache/'ordinary'
    else:
        target = clean.service.workspace/('staging-'+'a'*32)
    target.mkdir(exist_ok=True)
    (target/'README.md').write_text('Synthetic guard regression; must not be deleted.\n')
    sentinel = target/'keep.txt'; sentinel.write_text('keep')
    with pytest.raises(CleaningError) as blocked:
        clean.service._discard_staging(target)
    assert blocked.value.code == 'staging_cleanup_target_invalid' and sentinel.read_text() == 'keep'


def test_staging_cleanup_allows_only_a_generated_direct_child(clean):
    target = clean.service.cache/('staging-'+'b'*32)
    target.mkdir()
    (target/'README.md').write_text('Synthetic disposable staging child.\n')
    (target/'temporary.txt').write_text('synthetic')
    clean.service._discard_staging(target)
    assert not target.exists() and clean.service.cache.is_dir()
