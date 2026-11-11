from media_tag.precision.projection import verified as verified_provider


def verified(router,row,asset):
    return verified_provider(router,row,asset,'audio',layer='local_audio')
