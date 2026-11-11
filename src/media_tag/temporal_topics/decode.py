from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import json
from math import gcd,lcm

from PIL import Image,ImageOps,__version__ as pillow_version
from media_tag.image_processing.animation import decode as decode_animation
from media_tag.image_processing.models import ProcessingLimits
from .contracts import Pending


def model_image(frame,edge):
    rgba=frame.convert('RGBA')
    base=Image.new('RGBA',rgba.size,(255,255,255,255));base.alpha_composite(rgba)
    fitted=ImageOps.contain(base.convert('RGB'),(edge,edge),Image.Resampling.LANCZOS)
    canvas=Image.new('RGB',(edge,edge),'white')
    canvas.paste(fitted,((edge-fitted.width)//2,(edge-fitted.height)//2))
    data=BytesIO();canvas.save(data,format='PNG')
    return data.getvalue()


def grid(durations,maximum):
    denominator=lcm(*(d.denominator for d in durations))
    ticks=[d.numerator*(denominator//d.denominator) for d in durations]
    step=Fraction(gcd(*ticks),denominator)
    count=sum(int(d/step) for d in durations)
    if count>maximum:raise Pending('continuous_time_grid_requires_segmentation')
    if count<2:raise Pending('multiple_continuous_frames_required')
    return step,tuple(index for index,duration in enumerate(durations) for _ in range(int(duration/step)))


def animation(raw,options):
    try:
        decoded=decode_animation(raw,ProcessingLimits(max_input_bytes=options.max_input_bytes,
            max_frames=options.max_source_frames+1,max_pixels=options.max_source_pixels,
            max_total_pixels=options.max_total_source_pixels))
    except ValueError as exc:
        pending={'frame_budget':'source_frames_require_segmentation','total_pixel_budget':'source_total_pixel_budget',
            'pixel_budget':'source_spatial_policy_required'}.get(str(exc))
        if pending:raise Pending(pending) from None
        raise
    if decoded.icc_profile:raise Pending('explicit_color_conversion_policy_required')
    if not any(role=='animation' for role in decoded.roles):raise Pending('continuous_source_required')
    rows=[];time=Fraction(0)
    for index,(frame,delay,role) in enumerate(zip(decoded.frames,decoded.delays,decoded.roles,strict=True)):
        if role=='poster':continue
        rows.append((frame,time,delay,None));time+=delay
    if len(rows)>options.max_source_frames:raise Pending('source_frames_require_segmentation')
    if time>options.max_seconds:raise Pending('duration_requires_segmentation')
    return rows,{'origin':Fraction(0),'duration':time,'time_base':None,'stream':None,
        'coverage':'one_complete_animation_cycle','plays':decoded.plays,
        'poster':sha256(decoded.frames[0].tobytes()).hexdigest() if decoded.poster else None,
        'decoder':'Pillow-'+pillow_version+'-animation-exact-apng-v1'}


def video(path,tools,options):
    info=tools.probe_run(path,timeout=options.tool_timeout_seconds,extra=['-show_streams','-show_format'])
    streams=[s for s in info.get('streams',[]) if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic')]
    if options.video_stream_index is None and len(streams)!=1:raise Pending('explicit_video_stream_required')
    stream=next((s for s in streams if options.video_stream_index is None or s['index']==options.video_stream_index),None)
    if stream is None:raise Pending('selected_video_stream_unavailable')
    try:
        time_base=Fraction(stream['time_base']);start=int(stream['start_pts']);duration_ticks=int(stream['duration_ts'])
        sar=Fraction(stream['sample_aspect_ratio'].replace(':','/'))
        width,height=int(stream['width']),int(stream['height'])
        if time_base<=0 or duration_ticks<=0:raise ValueError('clock')
    except (KeyError,TypeError,ValueError,ZeroDivisionError):raise Pending('exact_selected_stream_clock_required') from None
    if sar!=1:raise Pending('square_pixel_policy_required')
    if any(mark in stream.get('pix_fmt','') for mark in ('yuva','rgba','argb','bgra','gbrap')):
        raise Pending('alpha_video_policy_required')
    supported_pixels={'yuv420p','yuvj420p','yuv422p','yuvj422p','yuv444p','yuvj444p',
        'rgb24','bgr24','gbrp','gray','gray8','nv12','nv21','yuyv422','uyvy422'}
    if (stream.get('pix_fmt') not in supported_pixels
            or stream.get('color_transfer') in ('smpte2084','arib-std-b67')
            or stream.get('color_primaries')=='bt2020'
            or str(stream.get('color_space','')).startswith('bt2020')):
        raise Pending('explicit_video_color_conversion_policy_required')
    rotations=[s['rotation'] for s in stream.get('side_data_list',[]) if 'rotation' in s]
    rotation=Fraction(str(rotations[0] if rotations else stream.get('tags',{}).get('rotate',0)))
    if rotation.denominator!=1 or int(rotation)%90:raise Pending('rotation_policy_required')
    if int(rotation)%180:width,height=height,width
    duration=duration_ticks*time_base;origin=start*time_base
    if duration>options.max_seconds:raise Pending('duration_requires_segmentation')
    if width*height>options.max_source_pixels:raise Pending('source_pixel_budget')
    metadata=tools.probe_run(path,timeout=options.tool_timeout_seconds,extra=['-select_streams',str(stream['index']),
        '-show_frames','-show_entries','frame=best_effort_timestamp,pts,duration,pkt_duration,width,height'])
    frames=metadata.get('frames',[])
    if not 2<=len(frames)<=options.max_source_frames:raise Pending('source_frames_require_segmentation')
    if len(frames)*width*height>options.max_total_source_pixels:raise Pending('source_total_pixel_budget')
    if any(int(row.get('width',0))!=int(stream['width']) or int(row.get('height',0))!=int(stream['height']) for row in frames):
        raise Pending('dynamic_frame_geometry_requires_policy')
    try:pts=[int(row.get('best_effort_timestamp',row.get('pts'))) for row in frames]
    except (TypeError,ValueError):raise Pending('frame_pts_required') from None
    if pts[0]!=start or any(b<=a for a,b in zip(pts,pts[1:])):raise Pending('non_continuous_selected_clock')
    ends=pts[1:]+[start+duration_ticks]
    durations=[]
    for row,p,e in zip(frames,pts,ends,strict=True):
        if e<=p:raise Pending('invalid_frame_duration')
        reported=row.get('duration',row.get('pkt_duration'))
        if reported is not None and int(reported)!=e-p:raise Pending('packet_duration_gap_requires_policy')
        durations.append((e-p)*time_base)
    # Keep every decoded display frame. No fps filter, seeking or sparse sampling.
    output,_=tools.ffmpeg_run(['-v','error','-xerror','-protocol_whitelist','file,pipe','-i',str(path),
        '-map',f"0:{stream['index']}",'-an','-sn','-dn','-fps_mode','passthrough','-frames:v',str(options.max_source_frames+1),
        '-threads','1','-pix_fmt','rgba','-f','rawvideo','pipe:1'],timeout=options.tool_timeout_seconds,
        stdout_limit=options.max_total_source_pixels*4+1)
    frame_size=width*height*4
    if len(output)!=frame_size*len(pts):raise Pending('decoded_frame_count_or_geometry_mismatch')
    rows=[(Image.frombytes('RGBA',(width,height),output[i*frame_size:(i+1)*frame_size]),p*time_base-origin,d,p)
        for i,(p,d) in enumerate(zip(pts,durations,strict=True))]
    return rows,{'origin':origin,'duration':duration,'time_base':time_base,'stream':stream['index'],
        'coverage':'complete_selected_video_stream','plays':None,'poster':None,
        'decoder':'ffmpeg-continuous-passthrough-'+sha256(json.dumps(tools.identity,sort_keys=True).encode()).hexdigest()}
