"""Hard filters use observed metadata/human values; unknown is never false or zero."""
from media_tag.review.models import ReviewDeployment
from media_tag.review.policy import ReviewPolicy
from media_tag.terminology.resolve.models import QueryInputError


def validate_filters(search,filters):
    if not filters.usages:return
    policy=search.reviews.policy if search.reviews else ReviewPolicy(search.catalog.contract,ReviewDeployment())
    definition=policy.asset_fields.get('用途大类');options=policy.options(definition) if definition else None
    if not options or not set(filters.usages)<=set(options):raise QueryInputError('usages must be original closed contract options')


def passes(search,c,asset,document,technical,filters):
    if asset['asset_id'] in filters.exclude_asset_ids:return False
    if filters.media_kinds and technical.get('kind') not in filters.media_kinds:return False
    if filters.format_names and not set(filters.format_names)&set(technical.get('format_names',())):return False
    duration=technical.get('duration_seconds')
    for bound,lower in ((filters.min_file_duration_seconds,True),(filters.max_file_duration_seconds,False)):
        if bound is not None and (duration is None or (duration<bound if lower else duration>bound)):return False
    for key,bound in (('display_width',filters.min_display_width),('display_height',filters.min_display_height)):
        if bound is not None and (technical.get(key) is None or technical[key]<bound):return False
    if filters.has_audio is not None and technical.get('has_audio') is not filters.has_audio:return False
    if filters.has_transparent_pixels is not None and technical.get('has_transparent_pixels') is not filters.has_transparent_pixels:return False
    if filters.usages and not any(f['field']=='用途大类' and set(f['values'])&set(filters.usages) for f in document['fields']):return False
    if filters.require_redacted:
        transform=search.files._transform(c,asset['asset_id'])
        if not transform or not transform['recipe'].get('mask'):return False
    return True
