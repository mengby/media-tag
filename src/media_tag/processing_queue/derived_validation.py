"""Actual media validation for registered processing outputs, including APNG."""
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

from media_tag.image_processing.animation import decode
from media_tag.image_processing.models import ProcessingLimits
from media_tag.upload.media import validate_media


def validate_output(path, tools, config, report):
    is_image=config.operation=='image_mask' or config.recipe.content_kind in ('image','animation')
    if not is_image:
        validation=validate_media(path,tools,full_decode=True)
        expected=config.recipe.content_kind
        if validation['kind']!=expected:
            raise ValueError('actual output modality differs from processing recipe')
        return validation
    limits=config.limits
    maximum=min(limits.max_output_bytes,512*1024**2)
    if report.output_bytes>maximum:raise ValueError('image_registration_byte_budget')
    with Path(path).open('rb') as stream:raw=stream.read(maximum+1)
    if len(raw)!=report.output_bytes or sha256(raw).hexdigest()!=report.output.sha256:
        raise ValueError('output changed during actual image validation')
    image_limits=ProcessingLimits(max_input_bytes=maximum,max_output_bytes=maximum,max_pixels=limits.max_pixels,
        max_total_pixels=limits.max_total_pixels if config.operation=='image_mask' else limits.max_image_pixels_total,
        max_frames=limits.max_frames if config.operation=='image_mask' else limits.max_image_frames)
    decoded=decode(raw,image_limits)
    animated=any(role=='animation' for role in decoded.roles)
    kind='animation' if animated else 'image'
    if config.operation=='cleaning' and kind!=config.recipe.content_kind:
        raise ValueError('actual animation must not be registered as a still')
    delays=[None if delay is None else {'numerator':delay.numerator,'denominator':delay.denominator} for delay in decoded.delays]
    return {'kind':kind,'width':decoded.frames[0].width,'height':decoded.frames[0].height,
        'display_width':decoded.frames[0].width,'display_height':decoded.frames[0].height,
        'duration_seconds':float(sum((delay for delay in decoded.delays if delay is not None),Fraction())) if animated else None,
        'start_seconds':0.0 if animated else None,'has_audio':False,
        'transparency':{'has_transparent_pixels':any(frame.getchannel('A').getextrema()[0]<255 for frame in decoded.frames),'complete':True},
        'frame_count':len(decoded.frames),'animation_plays':decoded.plays,'frame_delays':delays,'frame_roles':list(decoded.roles),
        'frame_rgba_sha256':[sha256(frame.tobytes()).hexdigest() for frame in decoded.frames],
        'intake_decode':'full','validation_method':'pillow_full_frames','semantic_review':False,
        'raw_metadata':{'format':{'format_name':'apng' if animated and decoded.source_format=='PNG' else decoded.source_format.lower()},'streams':[]}}
