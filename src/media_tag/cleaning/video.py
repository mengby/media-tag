"""Frame-accurate physical clipping and explicit audio timing, not segment notes."""
import math
from fractions import Fraction
from types import SimpleNamespace

from media_tag.transforms.retime import tempo_chain
from media_tag.workers.face_track import frame_clock
from media_tag.workers.probe import finite_number
from .errors import CleaningError, CleaningPending


def select_clock(info, recipe, limits):
    streams = info['raw_metadata']['streams']
    videos = [s for s in streams if s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')]
    if recipe.video_stream_index is None and len(videos) != 1:
        raise CleaningPending('explicit video stream selection required', code='video_track_selection_required')
    video = next((s for s in videos if recipe.video_stream_index is None or s['index'] == recipe.video_stream_index), None)
    if video is None:
        raise ValueError('selected video stream unavailable')
    origin, duration = finite_number(video.get('start_time')), finite_number(video.get('duration'))
    if origin is None or duration is None:
        raise CleaningPending('known bounded video stream clock required; container endpoints are not a duration',
            code='video_clock_unavailable', state='waiting_evidence')
    if duration <= 0 or duration > limits.max_seconds:
        raise CleaningError('known bounded video stream clock required; container endpoints are not a duration', code='video_duration_budget')
    try:
        sar = Fraction(str(video['sample_aspect_ratio']).replace(':', '/'))
        if sar <= 0:
            raise ValueError('unknown sample aspect ratio')
    except (KeyError, ValueError, ZeroDivisionError):
        raise CleaningPending('known video sample aspect ratio required',
            code='sample_aspect_ratio_unavailable', state='waiting_evidence') from None
    if sar != 1:
        raise CleaningPending('non-square source pixels require an explicit display-aspect-ratio policy',
            code='sample_aspect_ratio_policy_required')
    if any(token in video.get('pix_fmt', '') for token in ('yuva', 'rgba', 'argb', 'bgra', 'gbrap')):
        raise CleaningPending('transparent video requires an explicit alpha-capable renderer', code='alpha_renderer_required')
    if int(video.get('width', 0)) % 2 or int(video.get('height', 0)) % 2:
        raise CleaningPending('odd dimensions require an explicit padding or alternate-codec policy', code='dimensions_policy_required')
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    selected_audio = None
    if audio and recipe.audio_policy is None:
        raise CleaningPending('source has audio; choose preserve_pitch or remove explicitly', code='audio_policy_required')
    if recipe.audio_policy == 'preserve_pitch' and audio:
        if len(audio) > 1 and recipe.audio_stream_index is None:
            raise CleaningPending('multiple audio tracks require explicit selection', code='audio_track_selection_required')
        selected_audio = next((s for s in audio if recipe.audio_stream_index is None or s['index'] == recipe.audio_stream_index), None)
        if selected_audio is None:
            raise ValueError('selected audio stream unavailable')
        if finite_number(selected_audio.get('start_time')) is None or not (finite_number(selected_audio.get('duration')) or 0) > 0:
            raise CleaningPending('known selected audio stream clock required', code='audio_clock_unavailable', state='waiting_evidence')
        if recipe.segment is None and (float(selected_audio['start_time']) < origin-.0001
                or float(selected_audio['start_time'])+float(selected_audio['duration']) > origin+duration+.0001):
            raise CleaningPending('audio extends outside video; choose an explicit source segment instead of silently trimming it', code='segment_scope_required')
    elif recipe.audio_stream_index is not None:
        raise ValueError('selected audio stream unavailable')
    return video, selected_audio, origin, duration


def plan_video(path, tools, info, recipe, limits):
    video, audio, origin, duration = select_clock(info, recipe, limits)
    frames = frame_clock(path, tools, video['index'], origin=origin, video_end=origin+duration,
        options=SimpleNamespace(max_seconds=limits.max_seconds, max_frames=limits.max_frames))
    requested_start = recipe.segment.start_seconds if recipe.segment else 0.0
    requested_end = recipe.segment.end_seconds if recipe.segment else duration
    if requested_end > duration+.0001:
        raise ValueError('requested segment exceeds actual selected video stream')
    boundaries = [f['seconds'] for f in frames]+[duration]
    exact_start = next((t for t in boundaries if abs(t-requested_start) <= .0001), None)
    exact_end = next((t for t in boundaries if abs(t-requested_end) <= .0001), None)
    alignment = recipe.segment.alignment if recipe.segment else 'exact'
    if alignment == 'exact' and (exact_start is None or exact_end is None):
        raise CleaningPending('exact clipping requires source frame boundaries; choose inside alignment explicitly', code='frame_alignment_required')
    start = exact_start if exact_start is not None else min((t for t in boundaries if t >= requested_start), default=duration)
    end = exact_end if exact_end is not None else max((t for t in boundaries if t <= requested_end), default=0.0)
    chosen = [f for f in frames if f['seconds'] >= start-.000001 and f['seconds']+f['duration'] <= end+.000001]
    if not chosen or end <= start:
        raise ValueError('requested segment contains no complete source frame')
    start, end = chosen[0]['seconds'], chosen[-1]['seconds']+chosen[-1]['duration']
    speed = (end-start)/recipe.target_duration_seconds if recipe.target_duration_seconds is not None else recipe.speed
    if not math.isfinite(speed) or not .25 <= speed <= 4.0:
        raise CleaningPending('requested target duration exceeds supported speed range; choose another source segment', code='segment_speed_decision_required')
    output_duration = (end-start)/speed
    audio_plan = None
    if audio is not None:
        audio_start = float(audio['start_time'])
        audio_end = audio_start+float(audio['duration'])
        overlap_start, overlap_end = max(origin+start, audio_start), min(origin+end, audio_end)
        if overlap_end > overlap_start:
            audio_plan = {'source_stream_index': audio['index'], 'absolute_start_seconds': overlap_start,
                'absolute_end_seconds': overlap_end, 'output_start_seconds': (overlap_start-origin-start)/speed,
                'output_duration_seconds': (overlap_end-overlap_start)/speed,
                'sample_rate': int(audio['sample_rate'])}
    return {'source_video_stream': video['index'], 'source_origin_seconds': origin,
        'source_stream_duration_seconds': duration, 'requested_start_seconds': requested_start,
        'requested_end_seconds': requested_end, 'source_start_seconds': start, 'source_end_seconds': end,
        'absolute_start_seconds': origin+start, 'absolute_end_seconds': origin+end,
        'speed': speed, 'output_duration_seconds': output_duration, 'audio': audio_plan,
        'frames': [dict(f, output_seconds=(f['seconds']-start)/speed, output_duration=f['duration']/speed) for f in chosen]}


def render_video(path, target, tools, recipe, plan, limits):
    frames = plan['frames']
    vf = (f"trim=start_frame={frames[0]['index']}:end_frame={frames[-1]['index']+1},"
          f"settb=expr=1/1000000,setpts=(PTS-{plan['absolute_start_seconds']:.9f}/TB)/{plan['speed']:.15g},setsar=1")
    filters = [f"[0:{plan['source_video_stream']}]{vf}[v]"]
    audio = plan['audio']
    if audio is not None:
        af = (f"atrim=start={audio['absolute_start_seconds']:.9f}:end={audio['absolute_end_seconds']:.9f},"
              f"asetpts=PTS-STARTPTS,{tempo_chain(plan['speed'])},apad,"
              f"atrim=duration={audio['output_duration_seconds']:.9f},"
              f"asetpts=PTS-STARTPTS+{audio['output_start_seconds']:.9f}/TB")
        filters.append(f"[0:{audio['source_stream_index']}]{af}[a]")
    args = ['-v', 'error', '-xerror', '-copyts', '-protocol_whitelist', 'file,pipe', '-i', str(path),
        '-filter_complex', ';'.join(filters), '-map', '[v]', '-c:v', 'libx264', '-threads', '1', '-bf', '0',
        '-crf', str(recipe.crf), '-preset', recipe.preset, '-pix_fmt', 'yuv420p', '-fps_mode', 'passthrough',
        '-enc_time_base', '1:1000000', '-video_track_timescale', '1000000', '-frames:v', str(len(frames)),
        '-bsf:v', f"setts=duration={max(1, round(frames[-1]['duration']/plan['speed']*1000000))}"]
    args += ['-map', '[a]', '-c:a', 'aac'] if audio is not None else ['-an']
    args += ['-map_metadata', '-1', '-metadata:s:v:0', 'rotate=0', '-movflags', '+faststart',
        '-fs', str(limits.max_output_bytes), str(target)]
    tools.ffmpeg_run(args, timeout=limits.timeout_seconds)
    decoded = tools.probe_run(target, timeout=limits.timeout_seconds, extra=['-select_streams', 'v:0', '-show_frames',
        '-show_entries', 'frame=best_effort_timestamp_time,duration_time'])['frames']
    if len(decoded) != len(frames):
        raise ValueError('actual output frame count differs from selected source segment')
    for row, expected in zip(decoded, frames):
        if abs(float(row['best_effort_timestamp_time'])-expected['output_seconds']) > .0001 or abs(float(row['duration_time'])-expected['output_duration']) > .0001:
            raise ValueError('actual output frame time mapping differs from selected segment')
    info = tools.probe_run(target, timeout=limits.timeout_seconds)
    out_audio = [s for s in info['streams'] if s.get('codec_type') == 'audio']
    if len(out_audio) != int(audio is not None):
        raise ValueError('actual output audio layout differs from explicit policy')
    checks = []
    if audio is not None:
        track = out_audio[0]
        tolerance = .02+2048/int(track['sample_rate'])
        actual_start, actual_duration = float(track['start_time']), float(track['duration'])
        if abs(actual_start-audio['output_start_seconds']) > tolerance or abs(actual_duration-audio['output_duration_seconds']) > tolerance:
            raise ValueError('actual output audio timing differs from selected source overlap')
        checks.append(dict(audio, actual_start_seconds=actual_start, actual_duration_seconds=actual_duration,
            tolerance_seconds=tolerance, tempo_filter=tempo_chain(plan['speed'])))
    tools.ffmpeg_run(['-v', 'error', '-xerror', '-i', str(target), '-map', '0:v', '-map', '0:a?', '-f', 'null', '-'], timeout=limits.timeout_seconds)
    return {'decoded_frame_count': len(decoded), 'full_output_decode': True, 'audio_checks': checks,
        'video_encoder_threads': 1,
        'actual_output_start_seconds': float(decoded[0]['best_effort_timestamp_time']),
        'actual_output_duration_seconds': float(decoded[-1]['best_effort_timestamp_time'])+float(decoded[-1]['duration_time'])}
