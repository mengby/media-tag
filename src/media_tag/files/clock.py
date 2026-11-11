"""Narrow sample-index clock for verified single-stream integer PCM WAV metadata."""
from fractions import Fraction
import math


INTEGER_PCM={'pcm_u8','pcm_s16le','pcm_s24le','pcm_s32le','pcm_s64le'}


def _integer(value):
    if type(value) is int:return value
    if isinstance(value,str) and value.isascii() and value.isdecimal() and len(value)<=20:return int(value)
    return None


def pcm_wav_sample_clock(data):
    """No content scan or decimal-duration inference; missing old fields stay unknown."""
    if data.get('kind')!='audio':return None
    raw=data.get('raw_metadata',{})
    if raw.get('format',{}).get('format_name')!='wav':return None
    streams=raw.get('streams',[])
    if len(streams)!=1:return None
    stream=streams[0]
    if stream.get('codec_type')!='audio' or stream.get('codec_name') not in INTEGER_PCM:return None
    rate,count,channels=(_integer(stream.get(key)) for key in ('sample_rate','duration_ts','channels'))
    if (rate is None or not 1<=rate<=2**31-1 or count is None or not 1<=count<=2**63-1
            or channels is None or not 1<=channels<=65535):return None
    if stream.get('start_time') not in (None,'N/A'):return None
    if stream.get('start_pts') not in (None,'N/A') and _integer(stream['start_pts'])!=0:return None
    try:
        time_base=Fraction(stream.get('time_base',''))
        if time_base!=Fraction(1,rate):return None
        exact_duration=count*time_base;duration=float(exact_duration)
        if not math.isfinite(duration) or duration<=0:return None
    except (ValueError,TypeError,ZeroDivisionError,OverflowError):return None
    return {'normalization_origin_seconds':0.0,'duration_seconds':duration,
        'kind':'current_file_normalized_seconds','origin_basis':'single_stream_integer_pcm_wav_sample_index',
        'sample_rate':rate,'sample_count':count,'sample_time_base':str(time_base),
        'duration_rational':{'numerator':exact_duration.numerator,'denominator':exact_duration.denominator}}
