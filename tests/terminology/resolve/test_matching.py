import json
import pytest
from media_tag.terminology.contracts import Lexicon
from media_tag.terminology.resolve.models import ResolveInput
from media_tag.terminology.resolve.matching import exact_matches,normalized_offsets
from ..lexicon.conftest import alias,concept,lexicon


def query(resolved,text,**changes):
    return resolved.resolver.resolve(ResolveInput(query=text,**changes),principal=resolved.env.owner)


def test_exact_names_aliases_multi_meanings_and_relations(resolved):
    assert query(resolved,'合成圆形')['state']=='resolved'
    result=query(resolved,'寻找合成圆圈')
    assert result['state']=='resolved' and result['candidates'][0]['path']=='exact_alias'
    assert query(resolved,'多义合成词')['state']=='ambiguous'
    for text,relation in [('相关说法','related'),('像圆球一样','metaphor')]:
        result=query(resolved,text)
        assert result['state']=='ambiguous' and result['candidates'][0]['relation']==relation
        assert not result['candidates'][0]['default_expand']


@pytest.mark.parametrize('text,polarity,direction',[
    ('没有合成圆形','negated','unspecified'),
    ('合成圆形加重','affirmed','increasing'),
    ('合成圆形缓解','affirmed','decreasing'),
    ('合成圆形不加重','affirmed','unspecified'),
    ('合成圆形保持稳定','affirmed','static'),
])
def test_negation_and_direction_are_not_cleaned_away(resolved,text,polarity,direction):
    result=query(resolved,text);meaning=result['candidates'][0]['intent']
    assert meaning['polarity']==polarity and meaning['direction']==direction
    assert result['literal_query']==text
    if polarity=='negated' or direction in ('increasing','decreasing'):assert result['state']=='ambiguous'


def test_directions_and_negation_stay_in_their_clause(resolved):
    result=query(resolved,'没有合成圆形，合成方形加重')
    meanings={m['concept_id']:m['intent'] for m in result['matches']}
    assert meanings['concept-a']['polarity']=='negated' and meanings['concept-a']['direction']=='unspecified'
    assert meanings['concept-b']['polarity']=='affirmed' and meanings['concept-b']['direction']=='increasing'


def test_context_required_excluded_and_explicit_matching(resolved):
    assert query(resolved,'局部说法')['state']=='ambiguous'
    assert query(resolved,'局部说法',context=__import__('media_tag.terminology.contracts',fromlist=['Context']).Context(organs=('organ',)))['state']=='resolved'
    from media_tag.terminology.contracts import Context
    with pytest.raises(ValueError):query(resolved,'局部说法',context=Context(organs=('department',)))
    result=query(resolved,'合成圆形',context=Context(polarity='negated'))
    assert result['state']=='ambiguous' and result['candidates'][0]['intent']['polarity']=='negated'


def test_original_unicode_offsets_overlap_and_ascii_boundaries(resolved):
    raw='　Ａß  café\u0301 合成圆形'
    normalized,positions=normalized_offsets(raw)
    assert normalized=='ass café́ 合成圆形' and positions is not None
    payload=lexicon(resolved.tax['release_id'],concepts=[concept(name='café'),concept('concept-b',name='合成圆形')],
        aliases=[alias(expression='合成圆形的变化'),alias('other',expression='圆形',target_id='concept-b')])
    matches,_=exact_matches(payload,ResolveInput(query='合成圆形的变化'))
    assert {'合成圆形','圆形','合成圆形的变化'}<={m['raw_span'] for m in matches}
    ascii_payload=lexicon(resolved.tax['release_id'],concepts=[concept(name='art')],aliases=[])
    assert exact_matches(ascii_payload,ResolveInput(query='heart'))[0]==[]
    assert exact_matches(ascii_payload,ResolveInput(query='ＡＲＴ'))[0][0]['raw_span']=='ＡＲＴ'


def test_bge_only_proposes_and_refuses_low_cosine(resolved):
    result=query(resolved,'新的近义表达')
    assert result['state']=='ambiguous' and result['model_state']=='used'
    assert result['candidates'][0]['score_kind']=='cosine_not_medical_probability'
    assert not result['candidates'][0]['default_expand']
    result=query(resolved,'完全不相关的输入')
    assert result['state']=='no_match' and not result['candidates'] and result['reason']=='below_rejection_threshold'
    assert not query(resolved,'任何文本',concept_types=('organ',))['candidates']


@pytest.mark.parametrize('code',['text_model_missing','text_model_timeout','text_token_budget'])
def test_model_failure_is_degraded_not_no_match_and_exact_still_works(resolved,code):
    resolved.encoder.fail=code
    result=query(resolved,'模型未命中的合成新词')
    assert result['state']=='degraded' and code in result['errors'] and result['literal_search_available']
    assert query(resolved,'合成圆形')['state']=='resolved'
    assert query(resolved,'完全无确切短语',use_model=False)['state']=='no_match'


def test_cache_is_actor_context_version_bound_and_degraded_is_not_cached(resolved):
    request=ResolveInput(query='新的近义表达')
    first=resolved.resolver.resolve(request,principal=resolved.env.owner)
    assert not first['cache_hit']
    assert resolved.resolver.resolve(request,principal=resolved.env.owner)['cache_hit']
    assert not resolved.resolver.resolve(request,principal=resolved.env.other)['cache_hit']
    resolved.encoder.fail='text_model_timeout'
    assert query(resolved,'另一个近义表达')['state']=='degraded'
    resolved.encoder.fail=None
    assert query(resolved,'另一个近义表达')['model_state']=='used'


def test_a_negative_inside_the_whole_alias_is_not_erased(resolved):
    payload=lexicon(resolved.tax['release_id'],concepts=[concept(name='合成圆形')],aliases=[alias(expression='没有圆形')])
    matches,_=exact_matches(payload,ResolveInput(query='没有圆形'))
    assert matches[0]['intent']['polarity']=='negated' and not matches[0]['default_expand']
    result=query(resolved,'合成圆形没有缓解')
    assert result['state']=='ambiguous' and result['candidates'][0]['intent']['uncertain']


@pytest.mark.parametrize('vector',[[0.0]*512,[1.0]*511,[float('nan')]+[0.0]*511])
def test_invalid_model_outputs_degrade_without_entering_the_cache(resolved,vector):
    resolved.encoder.encode=lambda *args,**kwargs:[vector]
    result=query(resolved,'一个未收录表达')
    assert result['state']=='degraded' and result['errors']==['text_model_invalid_output']
    assert not result['cache_hit']
