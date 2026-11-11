"""Pure semantic modality projection from an already verified technical report.

The probe's raw kind/format and source evidence remain untouched. This helper is
not a media decoder and cannot qualify a caller-supplied, unverified report.
"""

KINDS = {'image':'image','video':'video','gif':'animation','animation':'animation','audio':'audio'}


def is_single_frame_gif(info):
    if not isinstance(info,dict) or info.get('kind')!='gif':return False
    raw=info.get('raw_metadata')
    image=raw.get('image_metadata') if isinstance(raw,dict) else None
    return (isinstance(image,dict) and image.get('format')=='GIF'
        and type(image.get('frame_count')) is int and image['frame_count']==1)


def semantic_content_kind(info):
    if not isinstance(info,dict) or info.get('kind') not in KINDS:
        raise ValueError('supported verified probe kind required')
    return 'image' if is_single_frame_gif(info) else KINDS[info['kind']]


def probe_kind_matches(info,declared_kind,*,allow_legacy_single_gif=False):
    current=semantic_content_kind(info)
    return current==declared_kind or (allow_legacy_single_gif and declared_kind=='animation' and is_single_frame_gif(info))
