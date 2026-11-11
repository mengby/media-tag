from media_tag.precision.service import request_provider


def request_audio_topics(analysis,deployment,asset_id,spec,*,principal):
    """Explicit authorized request; deployment is the existing local provider protocol."""
    return request_provider(analysis,deployment,asset_id,spec,principal=principal,stage='audio_topics')


def dispatch_pending(analysis,deployment,*,principal):
    from media_tag.precision.service import dispatch_pending as dispatch_provider
    return dispatch_provider(analysis,deployment,principal=principal,stage='audio_topics')
