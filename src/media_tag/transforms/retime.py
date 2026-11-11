"""Whole-file, fixed-speed CPU transforms using the registered common source clock."""
import math
from media_tag.models.contracts import ModelFailure
from media_tag.workers.face_track import frame_clock, check_display_budget
from media_tag.workers.probe import file_sha256


def tempo_chain(speed):
    if not math.isfinite(speed) or not .25 <= speed <= 4:
        raise ValueError('speed outside the fixed-speed contract')
    factors=[]; remaining=speed
    while remaining>2:
        factors.append(2.); remaining/=2
    while remaining<.5:
        factors.append(.5); remaining/=.5
    if remaining!=1 or not factors:
        factors.append(remaining)
    return ','.join(f'atempo={v:.15g}' for v in factors)


def mapped_segments(contract):
    if str(contract.get('schema_version','')).startswith('ct-processing-variant-'):
        from media_tag.catalog.service import encode
        from media_tag.processing_queue.derived_contracts import ProcessingVariantContract
        ProcessingVariantContract.model_validate_json(encode(contract))
        # The cleaning crop already exists in the current source bytes. Its
        # real source/output map is not the legacy named annotation contract.
        return []
    speed=contract['time_map']['speed']
    return [dict(segment_id=s['segment_id'],source_start_seconds=s['start_seconds'],source_end_seconds=s['end_seconds'],
                 output_start_seconds=s['start_seconds']/speed,output_end_seconds=s['end_seconds']/speed)
            for s in contract['spec']['recipe'].get('segments',[])]


def encode_video(path, output, tools, contract, frames, *, frame_paths=None, filename='retimed.mp4'):
    speed=contract['time_map']['speed']; recipe=contract['spec']['recipe']
    origin=contract['time_map']['clock']['origin_seconds']; index=contract['source_video_stream']
    prefix=['-v','error','-xerror','-copyts']
    if frame_paths is not None:
        concat=output/'frames.ffconcat'; lines=['ffconcat version 1.0']
        for frame,image in zip(frames,frame_paths):
            lines += [f"file 'frames/{image.name}'",'option framerate 1000000',f"duration {frame['duration']:.9f}"]
        lines += [f"file 'frames/{frame_paths[-1].name}'",'option framerate 1000000']
        concat.write_text('\n'.join(lines)+'\n',encoding='utf-8')
        prefix += ['-f','concat','-safe','0','-protocol_whitelist','file,pipe','-i',str(concat)]
        video_filter=f"settb=expr=1/1000000,setpts=(PTS+{frames[0]['seconds']:.9f}/TB)/{speed:.15g},setsar=1"
        video_map='0:v:0'
    else:
        prefix += ['-protocol_whitelist','file,pipe','-i',str(path)]
        video_filter=f'settb=expr=1/1000000,setpts=(PTS-{origin:.9f}/TB)/{speed:.15g},setsar=1'
        video_map=f'0:{index}'
    target=output/filename; audio=contract['selected_audio_stream']
    video_target=output/'video-only.mp4' if audio is not None else target
    tools.ffmpeg_run(prefix+['-map',video_map,'-vf',video_filter,'-c:v','libx264','-bf','0','-crf',str(recipe['crf']),
        '-preset',recipe['preset'],'-pix_fmt','yuv420p','-fps_mode','passthrough','-enc_time_base','1:1000000',
        '-video_track_timescale','1000000','-frames:v',str(len(frames)),'-bsf:v',
        f"setts=duration={max(1,round(frames[-1]['duration']/speed*1000000))}",'-an',
        '-map_metadata','-1','-metadata:s:v:0','rotate=0','-movflags','+faststart',str(video_target)],timeout=120)
    if audio is not None:
        expected_duration=contract['stream_durations'][str(audio)]/speed
        offset=contract['stream_offsets'][str(audio)]/speed
        audio_filter=f'asetpts=PTS-STARTPTS,{tempo_chain(speed)},apad,atrim=duration={expected_duration:.9f},asetpts=PTS-STARTPTS+{offset:.9f}/TB'
        tools.ffmpeg_run(['-v','error','-xerror','-copyts','-protocol_whitelist','file,pipe','-i',str(video_target),
            '-protocol_whitelist','file,pipe','-i',str(path),'-map','0:v:0','-map',f'1:{audio}','-c:v','copy',
            '-af',audio_filter,'-c:a','aac','-map_metadata','-1','-metadata:s:v:0','rotate=0','-movflags','+faststart',str(target)],timeout=120)
    decoded=tools.probe_run(target,timeout=120,extra=['-select_streams','v:0','-show_frames','-show_entries',
        'frame=best_effort_timestamp_time,duration_time'])['frames']
    if len(decoded)!=len(frames) or any(abs(float(row['best_effort_timestamp_time'])-f['seconds']/speed)>.0001 or
        abs(float(row['duration_time'])-f['duration']/speed)>.0001 for row,f in zip(decoded,frames)):
        raise ModelFailure('output_frame_timing_mismatch')
    info=tools.probe_run(target,timeout=20)
    audio_checks=[]
    actual_audio=[s for s in info['streams'] if s.get('codec_type')=='audio']
    if len(actual_audio)!=int(audio is not None): raise ModelFailure('output_audio_layout_mismatch')
    if audio is not None:
        track=actual_audio[0]; tolerance=.01+2048/int(track['sample_rate'])
        expected_start=contract['stream_offsets'][str(audio)]/speed
        expected_duration=contract['stream_durations'][str(audio)]/speed
        actual_start=float(track['start_time']); actual_duration=float(track['duration'])
        if abs(actual_start-expected_start)>tolerance or abs(actual_duration-expected_duration)>tolerance:
            raise ModelFailure('output_audio_timing_mismatch')
        audio_checks.append(dict(source_stream_index=audio,output_stream_index=track['index'],
            expected_start_seconds=expected_start,actual_start_seconds=actual_start,expected_duration_seconds=expected_duration,
            actual_duration_seconds=actual_duration,tolerance_seconds=tolerance,sample_rate=int(track['sample_rate']),
            tempo_filter=tempo_chain(speed),tail_policy='pad_or_trim_to_registered_scaled_duration'))
    tools.ffmpeg_run(['-v','error','-xerror','-i',str(target),'-map','0:v','-map','0:a?','-f','null','-'],timeout=120)
    return {'schema_version':'d11-retime-report-v1','source_sha256':file_sha256(path),'output_sha256':file_sha256(target),
        'speed':speed,'time_map':contract['time_map'],'cumulative_speed':contract['cumulative_speed'],
        'frames':[dict(source_pts=f['source_pts'],source_seconds=f['seconds'],source_duration=f['duration'],
                       output_seconds=f['seconds']/speed,output_duration=f['duration']/speed) for f in frames],
        'segments':mapped_segments(contract),'segments_are_annotations_only':True,'audio':audio_checks,
        'decoded_frame_count':len(decoded),'full_output_decode':True,'interpolation':False,'video_encode_passes':1,
        'privacy_confirmation':False,'medical_confirmation':False}


def retime(path, output, tools, contract, options):
    if contract['time_map']['clock']['duration_seconds']>options.max_seconds:
        raise ModelFailure('duration_budget')
    origin=contract['time_map']['clock']['origin_seconds']; index=contract['source_video_stream']
    end=origin+contract['stream_offsets'][str(index)]+contract['stream_durations'][str(index)]
    frames=frame_clock(path,tools,index,origin=origin,video_end=end,options=options)
    check_display_budget(path,tools,index,frames,contract['source_display'],options)
    return encode_video(path,output,tools,contract,frames)
