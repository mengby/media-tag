from media_tag.precision.projection import verified as verified_provider


def verified(router,row,asset):
    return verified_provider(router,row,asset,'audio',layer='local_audio')


def merge(router,connection,asset,kind,job,fields,evidence,producers):
    if kind!='audio':
        return fields,evidence,producers,{}
    from media_tag.precision.projection import merge as merge_provider
    return merge_provider(router,connection,asset,kind,job,fields,evidence,producers,layer='local_audio')
