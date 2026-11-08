from media_tag.terminology.resolve.models import ResolveInput


def test_threshold_or_latency_policy_change_invalidates_version_and_cache(resolved):
    resolver=resolved.resolver;principal=resolved.env.owner
    request=ResolveInput(query='新的合成描述')
    first=resolver.resolve(request,principal=principal)
    assert resolver.resolve(request,principal=principal)['cache_hit']
    resolver.options=resolver.options.model_copy(update={'min_cosine':.8,'query_timeout_seconds':7.0})
    second=resolver.resolve(request,principal=principal)
    assert first['versions']['resolution_policy_sha256']!=second['versions']['resolution_policy_sha256']
    assert not second['cache_hit'] and second['versions']['embedding_index_id']==first['versions']['embedding_index_id']
