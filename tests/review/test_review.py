import json
import pytest
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.review.models import Observation,FieldDecision,Confirmation
from .conftest import current,observation,decide


def test_manual_values_server_identity_proof_states_and_cas(reviewed):
    env=reviewed;obs=observation(env);before=current(env)
    result=decide(env,'画面主体',values=('Synthetic red sample',),obs=obs)
    after=current(env)
    assert after.revision==before.revision+1 and after.effective_values[0].actor_id==env.owner.actor_id
    assert after.effective_values[0].evidence_ids==(obs,) and result['medical_final_review'] is False
    bad=FieldDecision(request_id='stale',expected_revision=before.revision,source_sha256=before.sha256,field='画面主体',
        value={'state':'observed','values':('different',)},observation_ids=(obs,),note='Synthetic stale decision')
    with pytest.raises(RevisionConflict):env.reviews.decide(after.asset_id,bad,principal=env.owner)
    for state in ('unknown','no_match','failed','not_applicable'):
        decide(env,'画面主体',state=state,obs=obs)
        assert current(env).effective_values[0].value.state==state
    history=env.catalog.history(after.asset_id,principal=env.owner)
    last=history[-1]
    assert json.loads(last['before_json'])['values'][0]['value']['state']=='failed'
    assert json.loads(last['after_json'])['values'][0]['value']['state']=='not_applicable'
    assert len(env.reviews.detail(after.asset_id,principal=env.owner)['decisions'])==5
    assert (env.originals/'synthetic.png').read_bytes()==env.data


@pytest.mark.parametrize('actor',('reader','worker','other'))
def test_http_roles_and_object_permission_are_enforced(reviewed,actor):
    env=reviewed;client=getattr(env.clients,actor)
    assert client.get('/review/assets/'+env.asset.asset_id).status_code==403
    assert client.post('/review/assets/'+env.asset.asset_id+'/fields',json={}).status_code in (403,422)
    assert client.post('/review/exports',json={'request_id':'no','assets':[{'asset_id':env.asset.asset_id,'expected_revision':2,'source_sha256':env.asset.sha256}]}).status_code==403


def test_medical_no_automatic_role_or_unpublished_choices_and_no_batch_fields(reviewed):
    env=reviewed;obs=observation(env)
    with pytest.raises(AccessDenied):decide(env,'科室',values=('五官科',),obs=obs)
    expert=observation(env,actor=env.reviewer)
    with pytest.raises(ValueError,match='published'):decide(env,'科室',values=('五官科',),actor=env.reviewer,obs=expert)
    decide(env,'科室',state='no_match',actor=env.reviewer,obs=expert)
    asset=current(env)
    response=env.clients.reviewer.post('/review/assets/'+asset.asset_id+'/fields',json={'request_id':'bulk','expected_revision':asset.revision,
        'source_sha256':asset.sha256,'fields':['科室','疾病/症状'],'note':'Synthetic invalid bulk'})
    assert response.status_code==422
    with pytest.raises(ValueError):decide(env,'过程/状态',values=('正常→肿胀',),actor=env.reviewer,obs=expert)


@pytest.mark.parametrize('field,value', [('SHA256','0'*64),('标签状态','已人工确认'),('打标人','fake'),('素材ID','fake'),('素材类型','invented')])
def test_controlled_fields_and_locked_enums_cannot_be_forged(reviewed,field,value):
    with pytest.raises(ValueError):decide(reviewed,field,values=(value,))


def test_observation_hash_media_type_and_source_record_constraints(reviewed):
    env=reviewed;asset=current(env)
    spec=Observation(request_id='fake',expected_revision=asset.revision,source_sha256='0'*64,kind='image',note='Synthetic wrong hash',viewed=True)
    with pytest.raises(Exception):env.reviews.observe(asset.asset_id,spec,principal=env.owner)
    image=observation(env)
    with pytest.raises(ValueError):decide(env,'来源',values=('历史库',),obs=image)
    record=observation(env,kind='human_record',reference='https://example.invalid/original')
    with pytest.raises(ValueError):decide(env,'含人脸',values=('无',),obs=record)
    decide(env,'许可状态',values=('未核实',),obs=record)
    no_reference=observation(env,kind='human_record')
    with pytest.raises(ValueError):decide(env,'许可状态',values=('已核实',),obs=no_reference)
    bad=Observation(request_id='signed',expected_revision=current(env).revision,source_sha256=asset.sha256,kind='human_record',note='Synthetic signed source',reference='https://example.invalid/?token=secret')
    with pytest.raises(ValueError):env.reviews.observe(asset.asset_id,bad,principal=env.owner)


def test_idempotency_and_observation_cannot_cross_files(reviewed):
    env=reviewed;asset=current(env);obs=observation(env)
    spec=FieldDecision(request_id='same',expected_revision=asset.revision,source_sha256=asset.sha256,field='画面主体',
        value={'state':'observed','values':('Synthetic field',)},observation_ids=(obs,),note='Synthetic retry')
    one=env.reviews.decide(asset.asset_id,spec,principal=env.owner)
    assert env.reviews.decide(asset.asset_id,spec,principal=env.owner)==one
    with pytest.raises(RevisionConflict):env.reviews.decide(asset.asset_id,spec.model_copy(update={'note':'different'}),principal=env.owner)
    with pytest.raises(RevisionConflict):decide(env,'中文检索词',values=('synthetic',),obs='human-missing')


def test_index_confirmation_requires_complete_per_field_reviews_and_invalidates_on_change(reviewed):
    env=reviewed;actor=env.reviewer;obs=observation(env,actor=actor)
    def listing():
        result=env.clients.reviewer.get('/ui/library').json()
        assert result['medical_review']=='available'
        return next(v for item in result['items'] for v in item['versions'] if v['asset_id']==env.asset.asset_id)
    assert listing()['index_status']=='ai_draft'
    assert env.clients.owner.get('/ui/library').json()['medical_review']=='not_assigned'
    def confirm(request='confirm'):
        asset=current(env,actor)
        return env.reviews.confirm(asset.asset_id,Confirmation(request_id=request,expected_revision=asset.revision,source_sha256=asset.sha256,
            decision='confirm',full_source_viewed=True,note='Synthetic row status test only; not a production medical review'),principal=actor)
    with pytest.raises(ValueError):confirm()
    for field in env.reviews.policy.asset_fields.values():
        if field.fill_policy=='script' or field.name in ('标签状态','打标人','素材ID'):continue
        decide(env,field.name,state='not_applicable',actor=actor,obs=obs)
    confirmed=confirm()
    assert confirmed['index_status']=='confirmed' and current(env,actor).index_status=='confirmed'
    assert env.clients.reviewer.get('/v1/assets/'+env.asset.asset_id).json()['index_status']=='confirmed'
    assert listing()['index_status']==listing()['analysis']=='confirmed'
    decide(env,'画面主体',values=('Synthetic changed visual',),actor=actor,obs=obs)
    assert current(env,actor).index_status=='needs_review'
    assert listing()['index_status']==listing()['analysis']=='needs_review'
    assert env.clients.reviewer.get('/v1/assets/'+env.asset.asset_id).json()['current_use_review']=='not_checked'
    confirm('reconfirm')
    assert listing()['index_status']=='confirmed'
    asset=current(env,actor)
    env.reviews.confirm(asset.asset_id,Confirmation(request_id='revoke',expected_revision=asset.revision,source_sha256=asset.sha256,
        decision='revoke',note='Synthetic explicit index withdrawal'),principal=actor)
    assert listing()['index_status']==listing()['analysis']=='needs_review'


def test_reader_named_in_reviewer_configuration_does_not_gain_medical_capability(reviewed):
    env=reviewed
    from media_tag.review.service import ReviewService
    from media_tag.review.models import ReviewDeployment
    from media_tag.api.app import create_app
    from fastapi.testclient import TestClient
    from hashlib import sha256
    reviews=ReviewService(env.router,ReviewDeployment(enabled=True,reviewers=(env.reader.actor_id,)))
    app=create_app(env.router,{sha256(b'reader').hexdigest():env.reader},web=True,reviews=reviews)
    client=TestClient(app,headers={'Authorization':'Bearer reader'})
    assert client.get('/ui/session').json()['medical_review']=='not_assigned'
    assert client.get('/ui/library').json()['medical_review']=='not_assigned'
    assert client.get('/review/assets/'+env.asset.asset_id).status_code==403
