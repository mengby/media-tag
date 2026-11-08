import pytest
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.search.models import SearchRequest,ConceptLink,RevokeLink
from media_tag.terminology.resolve.models import ResolveInput
from media_tag.catalog.service import encode


def query(sought,text='合成圆圈',actor=None,**changes):
    if changes.get('selected_concept_ids'):changes.setdefault('selection_lexicon_release_id',sought.lex['release_id'])
    return sought.search.search(SearchRequest(request=ResolveInput(query=text),**changes),principal=actor or sought.env.owner)


def test_equivalent_lookup_explains_human_text_and_never_grants_permissions(sought):
    result=query(sought)
    assert result['resolution']['state']=='resolved' and len(result['items'])==1
    item=result['items'][0]['versions'][0]
    assert item['asset_id']==sought.asset.asset_id
    assert any(r['kind']=='expanded_human_text' and not r['medical_evidence'] for r in item['reasons'])
    assert not query(sought,actor=sought.env.other)['items']
    assert len(query(sought,actor=sought.env.reader)['items'])==1
    with pytest.raises(AccessDenied):query(sought,actor=sought.env.worker)
    assert sought.env.catalog.get_asset(sought.asset.asset_id,principal=sought.env.owner).revision==sought.asset.revision


def test_no_word_no_visible_asset_and_ambiguity_are_different(sought):
    assert query(sought,'方块说法')['empty_reason']=='recognized_no_visible_assets'
    assert query(sought,'完全不相关表达')['empty_reason']=='unrecognized_expression'
    assert query(sought,'多义说法')['empty_reason']=='choose_concept'
    result=query(sought,'多义说法',selected_concept_ids=('concept-a',))
    assert result['items'] and result['selected_concept_ids']==['concept-a']
    with pytest.raises(ValueError):query(sought,selected_concept_ids=('not-published',))


def test_negation_does_not_mean_missing_positive_tags(sought):
    result=query(sought,'没有合成方形',selected_concept_ids=('square',))
    assert not result['items']
    linked=sought.search.links.create(sought.asset.asset_id,ConceptLink(request_id='negated',expected_revision=sought.asset.revision,
        source_sha256=sought.asset.sha256,lexicon_release_id=sought.lex['release_id'],concept_id='square',polarity='negated',
        observation_ids=(sought.observation,),note='Synthetic scoped negative geometry annotation'),principal=sought.env.owner)
    result=query(sought,'没有合成方形',selected_concept_ids=('square',))
    assert result['items'] and any(r['polarity']=='negated' for r in result['items'][0]['versions'][0]['reasons'] if r['kind']=='human_concept')
    assert sought.search.links.create(sought.asset.asset_id,ConceptLink(request_id='negated',expected_revision=sought.asset.revision,
        source_sha256=sought.asset.sha256,lexicon_release_id=sought.lex['release_id'],concept_id='square',polarity='negated',
        observation_ids=(sought.observation,),note='Synthetic scoped negative geometry annotation'),principal=sought.env.owner)==linked


def test_concept_link_cannot_bypass_medical_fields_or_continuous_evidence(sought):
    spec=ConceptLink(request_id='invalid',expected_revision=sought.asset.revision,source_sha256=sought.asset.sha256,
        lexicon_release_id=sought.lex['release_id'],concept_id='clinical',observation_ids=(sought.observation,),note='Synthetic invalid medical link')
    with pytest.raises(ValueError,match='D06'):sought.search.links.create(sought.asset.asset_id,spec,principal=sought.env.owner)
    with pytest.raises(ValueError,match='continuous'):
        sought.search.links.create(sought.asset.asset_id,spec.model_copy(update={'concept_id':'concept-a','direction':'increasing'}),principal=sought.env.owner)


def test_link_revoke_changes_revision_and_stays_out_of_csv_fields(sought):
    spec=ConceptLink(request_id='link',expected_revision=sought.asset.revision,source_sha256=sought.asset.sha256,
        lexicon_release_id=sought.lex['release_id'],concept_id='concept-a',observation_ids=(sought.observation,),note='Synthetic geometry link')
    created=sought.search.links.create(sought.asset.asset_id,spec,principal=sought.env.owner)
    detail=sought.search.links.detail(sought.asset.asset_id,principal=sought.env.owner)
    assert detail['links'][0]['current'] and detail['revision']==created['revision']
    with sought.env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM effective_values').fetchone()[0]==1
    revoked=sought.search.links.revoke(sought.asset.asset_id,created['link_id'],RevokeLink(request_id='revoke',expected_revision=created['revision'],source_sha256=sought.asset.sha256,note='Synthetic withdrawal'),principal=sought.env.owner)
    assert revoked['revision']==created['revision']+1
    assert not sought.search.links.detail(sought.asset.asset_id,principal=sought.env.owner)['links'][0]['current']


def test_withdrawal_and_revocation_are_immediate_even_after_a_search(sought):
    assert query(sought,actor=sought.env.reader)['items']
    with sought.env.database.transaction(write=True) as c:c.execute('DELETE FROM asset_permissions WHERE asset_id=? AND subject_id=?',(sought.asset.asset_id,sought.env.reader.actor_id))
    assert not query(sought,actor=sought.env.reader)['items']
    with sought.env.database.transaction(write=True) as c:c.execute('UPDATE library_entries SET active=0 WHERE asset_id=?',(sought.asset.asset_id,))
    assert not query(sought)['items']
    assert query(sought,workspace=True)['items']


def test_medical_link_becomes_stale_when_its_confirmed_field_is_revised(sought):
    from media_tag.review.models import FieldDecision
    from media_tag.contracts.models import FieldValue
    env=sought.env
    first=sought.reviews.decide(sought.asset.asset_id,FieldDecision(request_id='synthetic-classification',expected_revision=sought.asset.revision,
        source_sha256=sought.asset.sha256,field='疾病/症状',value=FieldValue(state='observed',values=('合成分类-仅测试',)),
        observation_ids=(sought.observation,),note='Synthetic protocol field, not a real medical finding'),principal=env.owner)
    current=env.catalog.get_asset(sought.asset.asset_id,principal=env.owner)
    linked=sought.search.links.create(sought.asset.asset_id,ConceptLink(request_id='clinical-link',expected_revision=current.revision,
        source_sha256=current.sha256,lexicon_release_id=sought.lex['release_id'],concept_id='clinical',observation_ids=(sought.observation,),
        note='Synthetic protocol linkage only'),principal=env.owner)
    assert sought.search.links.detail(current.asset_id,principal=env.owner)['links'][0]['current']
    sought.reviews.decide(current.asset_id,FieldDecision(request_id='withdraw-clinical-value',expected_revision=linked['revision'],source_sha256=current.sha256,
        field='疾病/症状',value=FieldValue(state='unknown',reason='Synthetic withdrawal; no actual clinical assertion'),observation_ids=(sought.observation,),
        note='Synthetic field revision invalidates derived associations'),principal=env.owner)
    assert not sought.search.links.detail(current.asset_id,principal=env.owner)['links'][0]['current']


def test_file_gate_runs_before_metadata_and_draft_loading(sought,monkeypatch):
    monkeypatch.setattr(sought.search.files,'_gate',lambda c,asset:'privacy_review_required')
    def forbidden(*args):raise AssertionError('gated assets must not load search documents')
    monkeypatch.setattr(sought.search,'_document',forbidden)
    monkeypatch.setattr(sought.search,'_drafts',forbidden)
    assert not query(sought,include_drafts=True)['items']


def test_explicit_direction_filter_cannot_be_bypassed_by_literal_human_text(sought):
    from media_tag.terminology.contracts import Context
    spec=SearchRequest(request=ResolveInput(query='合成圆形',context=Context(direction='increasing')))
    assert not sought.search.search(spec,principal=sought.env.owner)['items']

    spec=SearchRequest(request=ResolveInput(query='合成圆形',context=Context(polarity='negated')))
    assert not sought.search.search(spec,principal=sought.env.owner)['items']


def test_department_filter_requires_the_confirmed_medical_field(sought):
    from media_tag.terminology.contracts import Context
    from media_tag.review.models import FieldDecision
    from media_tag.contracts.models import FieldValue
    spec=SearchRequest(request=ResolveInput(query='合成圆形',context=Context(departments=('department',))))
    assert not sought.search.search(spec,principal=sought.env.owner)['items']
    sought.reviews.decide(sought.asset.asset_id,FieldDecision(request_id='synthetic-department',expected_revision=sought.asset.revision,
        source_sha256=sought.asset.sha256,field='科室',value=FieldValue(state='observed',values=('合成科室-仅测试',)),
        observation_ids=(sought.observation,),note='Synthetic protocol field only'),principal=sought.env.owner)
    assert sought.search.search(spec,principal=sought.env.owner)['items']
