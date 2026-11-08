import json
import pytest
from media_tag.terminology.contracts import Taxonomy,Lexicon,Context,normalize
from media_tag.terminology.lexicon.validation import issues
from .conftest import taxonomy,lexicon,option,concept,alias,relation


def codes(payload,env,tax=None,previous=()):return {i['code'] for i in issues(payload,env.contract,tax,previous)}


@pytest.mark.parametrize('mutate,expected',[
    (lambda p:p['options'][0].update(sources=[]),'missing_source'),
    (lambda p:p['options'].append(p['options'][0].copy()),'duplicate_entry_id'),
    (lambda p:p['options'].append(option('other')),'duplicate_option_value'),
    (lambda p:p['options'][0].update(field='情绪'),'locked_template_field'),
    (lambda p:p['options'][0].update(value='锁定列表…'),'placeholder_option'),
    (lambda p:p.update(template_fingerprint='0'*64),'template_mismatch'),
    (lambda p:p['options'][0].update(replacements=['absent']),'invalid_migration_target'),
])
def test_taxonomy_validation(env,mutate,expected):
    data=taxonomy().model_dump(mode='json');mutate(data)
    assert expected in codes(Taxonomy.model_validate_json(json.dumps(data)),env)


@pytest.mark.parametrize('changes,expected',[
    ({'aliases':[alias(target_id=None)]},'missing_active_target'),
    ({'aliases':[alias(relation='related')]},'non_equivalent_expansion'),
    ({'aliases':[alias(relation='contextual',default_expand=False)]},'missing_context'),
    ({'aliases':[alias(context={'departments':['option-a']})]},'invalid_department_context'),
    ({'aliases':[alias(context={'required_terms':['甲'],'excluded_terms':['甲']})]},'contradictory_context'),
    ({'concepts':[concept(oid='absent')]},'unpublished_taxonomy_reference'),
    ({'concepts':[concept(concept_type='organ')]},'concept_type_mismatch'),
    ({'concepts':[concept(status='retired')]},'missing_active_target'),
    ({'aliases':[alias(),alias('alias-b')]},'duplicate_mapping'),
    ({'relations':[relation(source_id='concept-a',target_id='concept-a')]},'self_relation'),
])
def test_lexicon_validation(env,changes,expected):
    assert expected in codes(lexicon('synthetic-taxonomy',**changes),env,taxonomy())


def test_polysemy_is_explicit_and_context_is_preserved(env):
    cs=[concept(),concept('concept-b')]
    same=[alias(),alias('alias-b',target_id='concept-b')]
    assert 'unacknowledged_ambiguity' in codes(lexicon('tax',concepts=cs,aliases=same),env,taxonomy())
    for a in same:a.update(ambiguity_group='synthetic-ambiguity',default_expand=False)
    assert not codes(lexicon('tax',concepts=cs,aliases=same),env,taxonomy())
    contextual=[alias(context={'polarity':'negated','direction':'decreasing'}),alias('alias-b',target_id='concept-b',context={'polarity':'affirmed','direction':'increasing'})]
    payload=lexicon('tax',concepts=cs,aliases=contextual)
    assert not codes(payload,env,taxonomy())
    assert payload.aliases[0].context.polarity=='negated' and payload.aliases[1].context.direction=='increasing'
    assert normalize('  Ａ   没有加重→缓解 ')== 'a 没有加重→缓解'


def test_hierarchy_direction_cycles_and_related_no_expansion(env):
    cs=[concept(),concept('concept-b'),concept('concept-c')]
    relations=[relation(relation='broader'),relation('r2',source_id='concept-b',target_id='concept-c',relation='broader'),
               relation('r3',source_id='concept-c',target_id='concept-a',relation='broader')]
    assert 'hierarchy_cycle' in codes(lexicon('tax',concepts=cs,relations=relations),env,taxonomy())
    relations[2]['relation']='narrower'
    assert not codes(lexicon('tax',concepts=cs,relations=relations),env,taxonomy())
    relations[0].update(relation='related',default_expand=True)
    assert 'non_equivalent_expansion' in codes(lexicon('tax',concepts=cs,relations=relations),env,taxonomy())


def test_retirement_stable_ids_and_rename(env):
    old=taxonomy();retired=taxonomy(option(status='retired'))
    assert 'retired_id_reused' in codes(old,env,previous=(retired,))
    assert 'option_id_reused' in codes(taxonomy(option(value='另一个意思')),env,previous=(old,))
    assert 'retirement_required' in codes(taxonomy(option('different')),env,previous=(old,))
    oldlex=lexicon('tax');newlex=lexicon('tax',concepts=[concept(name='新标准名')])
    assert 'rename_requires_old_name' in codes(newlex,env,old,previous=(oldlex,))
    renamed=lexicon('tax',concepts=[concept(name='新标准名',old_names=['concept-a-合成'])])
    assert not codes(renamed,env,old,previous=(oldlex,))
    hanging=lexicon('tax',concepts=[concept(status='retired',replacements=['concept-b']),concept('concept-b')])
    assert 'missing_active_target' in codes(hanging,env,old)


def test_equivalence_does_not_override_different_taxonomy_meanings(env):
    payload=lexicon('tax',concepts=[concept(),concept('concept-b',oid='option-b')],relations=[relation(relation='equivalent',default_expand=True)])
    assert 'conflicting_equivalence' in codes(payload,env,taxonomy(option(),option('option-b',value='合成乙')))


def test_context_order_cannot_bypass_ambiguity_and_entity_ids_are_stable(env):
    payload=lexicon('tax',concepts=[concept(),concept('concept-b')],aliases=[
        alias(context={'required_terms':['Ａ','Ｂ']}),alias('alias-b',target_id='concept-b',context={'required_terms':['b','a']})])
    assert 'unacknowledged_ambiguity' in codes(payload,env,taxonomy())
    old=lexicon('tax');repurposed=lexicon('tax',aliases=[alias('other')],concepts=[concept(),concept('alias-a')])
    assert 'entity_id_reused' in codes(repurposed,env,taxonomy(),previous=(old,))


def test_sources_reject_signed_links_and_long_hierarchy_does_not_recurse(env):
    from pydantic import ValidationError
    from media_tag.terminology.contracts import Source
    from .conftest import SOURCE
    with pytest.raises(ValidationError):Source.model_validate(SOURCE|{'reference':'https://example.test/file?token=sensitive'})
    cs=[concept('c'+str(i)) for i in range(1100)]
    rs=[relation('r'+str(i),source_id='c'+str(i),target_id='c'+str(i+1),relation='broader') for i in range(1099)]
    assert not codes(lexicon('tax',concepts=cs,relations=rs,aliases=[]),env,taxonomy())
