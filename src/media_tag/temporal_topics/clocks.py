"""Explicit bridge from selected visual PTS to the current file's review clock."""
import math
from media_tag.models.contracts import ModelFailure
from .contracts import Rational


def common_clock(probe,kind):
    if kind=='animation':return Rational.of(0),None
    streams=[stream for stream in probe.get('raw_metadata',{}).get('streams',[])
        if stream.get('codec_type') in ('video','audio') and not stream.get('disposition',{}).get('attached_pic')]
    starts=[];ends=[]
    try:
        for stream in streams:
            start=float(stream['start_time']);duration=float(stream['duration'])
            if not math.isfinite(start) or not math.isfinite(duration) or duration<0:raise ValueError('clock')
            starts.append(start);ends.append(start+duration)
        if not starts:raise ValueError('clock')
    except (KeyError,TypeError,ValueError,OverflowError):
        raise ModelFailure('temporal_common_source_clock_required') from None
    return Rational.of(str(min(starts))),max(ends)-min(starts)


def check_window(sequence,origin,span):
    offset=sequence.source_origin.fraction()-origin.fraction()
    end=offset+sequence.duration.fraction()
    if offset<0 or span is not None and float(end)>span:
        raise ModelFailure('temporal_common_clock_bounds_required')
    return float(offset),float(end)
