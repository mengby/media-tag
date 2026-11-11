"""Real animation/report bindings with a synthetic subprocess model adapter."""
from hashlib import sha256
from io import BytesIO

from PIL import Image
import pytest

from precision.conftest import precise
from search.conftest import sought, env
from terminology.lexicon.conftest import option, publish, taxonomy
from media_tag.review.models import FieldDecision, Observation
from media_tag.review.service import ReviewService


def dynamic_case(p,*,value='收缩',dynamic=True):
    release=publish(p.env,taxonomy(option('purpose',field='用途大类',value='信息图解'),
        option('medical',value='合成分类-仅测试'),option('department',field='科室',value='合成科室-仅测试'),
        option('dynamic-process',field='过程/状态',value=value)),
        version='synthetic-dynamic-v2',base=p.sought.tax['release_id'])
    reviews=ReviewService(p.env.router,p.sought.reviews.config.model_copy(update={
        'taxonomy_manifest':str(p.env.workspace/release['manifest_ref']),
        'taxonomy_version':release['version'],'taxonomy_manifest_sha256':release['manifest_sha256']}))
    raw=BytesIO()
    Image.new('RGB',(24,24),'red').save(raw,format='GIF',save_all=True,
        append_images=[Image.new('RGB',(24,24),'blue')],duration=[100,100],loop=0)
    source=p.register(raw.getvalue(),'dynamic.gif','animation')
    deployment=p.deployment()
    p.bridge.write_text(p.bridge.read_text(encoding='utf8').replace(
        "fields.append({'field': name, 'value': value})",
        "fields.append({'field': name, 'value': {'state':'observed','values':["+repr(value)+"]}, 'dynamic':"+repr(dynamic)+"})"),encoding='utf8')
    provider=deployment.providers[0].model_copy(update={'adapter_sha256':sha256(p.bridge.read_bytes()).hexdigest()})
    deployment=deployment.model_copy(update={'providers':(provider,)})
    p.analysis.precision_deployment=deployment
    _,_,executor=p.submit(deployment,asset_id=source.asset_id,fields=('过程/状态',),request='dynamic-protocol')
    assert executor.run_once()['accepted']
    item=reviews.detail(source.asset_id,principal=p.env.owner)['suggestions'][0]
    observation=reviews.observe(source.asset_id,Observation(request_id='image-only',
        expected_revision=source.revision,source_sha256=source.sha256,kind='image',viewed=True,
        note='Synthetic still-style human observation of an animation'),principal=p.env.owner)['observation_id']
    spec=FieldDecision(request_id='accept-with-client-false',expected_revision=source.revision,
        source_sha256=source.sha256,field='过程/状态',decision='accept',run_id=item['run_id'],
        suggestion_id=item['suggestion_id'],observation_ids=(observation,),dynamic=False,
        note='Synthetic client attempts to downgrade a producer dynamic claim')
    return reviews,source,item,spec


def test_producer_dynamic_without_arrow_cannot_be_downgraded_by_client(precise):
    reviews,source,item,spec=dynamic_case(precise)
    assert item['producer_dynamic'] and item['requires_continuous_observation']
    with pytest.raises(ValueError,match='producer dynamic claim requires continuous video'):
        reviews.decide(source.asset_id,spec,principal=precise.env.owner)
    observed=reviews.observe(source.asset_id,Observation(request_id='continuous',
        expected_revision=source.revision,source_sha256=source.sha256,kind='video_segment',viewed=True,
        start_seconds=0.0,end_seconds=0.2,note='Synthetic continuous review of both animation frames'),
        principal=precise.env.owner)['observation_id']
    spec=spec.model_copy(update={'observation_ids':(observed,)})
    accepted=reviews.decide(source.asset_id,spec,principal=precise.env.owner)
    assert accepted['effective']['value']['values']==['收缩']
    with precise.env.database.transaction() as connection:
        row=connection.execute('SELECT payload_json FROM review_decisions WHERE decision_id=?',(accepted['decision_id'],)).fetchone()
    import json
    record=json.loads(row[0])
    assert record['dynamic'] is False and record['producer_dynamic_required'] is True
    assert record['producer_observation_ranges']==[[0.0,0.2]]
    assert len(record['producer_requirement_basis_sha256'])==64


def test_explicit_manual_value_keeps_existing_rule_without_guessing_dynamic(precise):
    reviews,source,_,_=dynamic_case(precise)
    observations=reviews.detail(source.asset_id,principal=precise.env.owner)['observations']
    from media_tag.contracts.models import FieldValue
    accepted=reviews.decide(source.asset_id,FieldDecision(request_id='manual-no-guessed-motion',
        expected_revision=source.revision,source_sha256=source.sha256,field='过程/状态',dynamic=False,
        value=FieldValue(state='observed',values=('收缩',)),observation_ids=(observations[0]['observation_id'],),
        note='Synthetic explicit manual input keeps the existing observation rule'),principal=precise.env.owner)
    assert accepted['effective']['value']['values']==['收缩']


def test_short_continuous_observation_cannot_cover_full_declared_dynamic_range(precise):
    reviews,source,item,spec=dynamic_case(precise)
    assert item['producer_dynamic_ranges']==[[0.0,0.2]]
    observed=reviews.observe(source.asset_id,Observation(request_id='short-interval',
        expected_revision=source.revision,source_sha256=source.sha256,kind='video_segment',viewed=True,
        start_seconds=0.0,end_seconds=0.01,note='Synthetic tiny interval does not cover declared source evidence'),
        principal=precise.env.owner)['observation_id']
    with pytest.raises(ValueError,match='covering declared evidence ranges'):
        reviews.decide(source.asset_id,spec.model_copy(update={'observation_ids':(observed,)}),principal=precise.env.owner)


@pytest.mark.parametrize('ranges,accepted',[
    (((0.0,0.1),(0.1,0.2)),True),
    (((0.0,0.12),(0.1,0.2)),True),
    (((0.0,0.09),(0.1,0.2)),False),
    (((0.01,0.2),),False),
])
def test_actual_adjacent_views_may_cover_dynamic_range_but_no_gaps(precise,ranges,accepted):
    reviews,source,_,spec=dynamic_case(precise)
    ids=[]
    for index,(start,end) in enumerate(ranges):
        ids.append(reviews.observe(source.asset_id,Observation(request_id='interval-'+str(index),
            expected_revision=source.revision,source_sha256=source.sha256,kind='video_segment',viewed=True,
            start_seconds=start,end_seconds=end,note='Synthetic bounded actual observation interval'),
            principal=precise.env.owner)['observation_id'])
    spec=spec.model_copy(update={'observation_ids':tuple(reversed(ids))})
    if accepted:
        receipt=reviews.decide(source.asset_id,spec,principal=precise.env.owner)
        assert receipt['effective']['value']['values']==['收缩']
    else:
        with pytest.raises(ValueError,match='covering declared evidence ranges'):
            reviews.decide(source.asset_id,spec,principal=precise.env.owner)


def test_other_actors_full_view_cannot_cover_current_reviewers_dynamic_requirement(precise):
    p=precise;reviews,source,_,spec=dynamic_case(p)
    p.env.catalog.change_access(source.asset_id,principal=p.env.owner,expected_revision=source.revision,
        subject_id=p.env.other.actor_id,permission='edit',reason='Synthetic separate observer permission')
    source=p.env.catalog.get_asset(source.asset_id,principal=p.env.owner)
    observed=reviews.observe(source.asset_id,Observation(request_id='other-continuous',
        expected_revision=source.revision,source_sha256=source.sha256,kind='video_segment',viewed=True,
        start_seconds=0.0,end_seconds=0.2,note='Synthetic other person full continuous observation'),
        principal=p.env.other)['observation_id']
    spec=spec.model_copy(update={'expected_revision':source.revision,
        'observation_ids':(*spec.observation_ids,observed)})
    with pytest.raises(ValueError,match='covering declared evidence ranges'):
        reviews.decide(source.asset_id,spec,principal=p.env.owner)


def test_explicit_producer_direction_also_requires_covering_its_declared_range(precise):
    reviews,source,item,spec=dynamic_case(precise,value='收缩→舒张',dynamic=False)
    assert item['requires_continuous_observation'] and item['producer_dynamic_ranges']==[[0.0,0.2]]
    observed=reviews.observe(source.asset_id,Observation(request_id='arrow-short',
        expected_revision=source.revision,source_sha256=source.sha256,kind='video_segment',viewed=True,
        start_seconds=0.0,end_seconds=0.01,note='Synthetic short observation for an explicit directional claim'),
        principal=precise.env.owner)['observation_id']
    with pytest.raises(ValueError,match='covering declared evidence ranges'):
        reviews.decide(source.asset_id,spec.model_copy(update={'observation_ids':(observed,)}),principal=precise.env.owner)
