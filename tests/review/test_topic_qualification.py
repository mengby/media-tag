from hashlib import sha256
import json

import pytest
from fastapi.testclient import TestClient

from precision.conftest import precise
from search.conftest import sought, env
from local_topics.test_router import topic_router
from media_tag.catalog import AccessDenied, RevisionConflict
from media_tag.contracts.models import FieldValue
from media_tag.review.models import FieldDecision


def finish(p,fields=None,request='review-precision',mode='normal'):
    deployment=p.deployment(mode)
    _,receipt,executor=p.submit(deployment,fields=fields,request=request)
    assert executor.run_once()['accepted']
    return receipt


def current(p):
    return p.env.catalog.get_asset(p.sought.asset.asset_id,principal=p.env.owner)


def detail(p):
    return p.sought.reviews.detail(p.sought.asset.asset_id,principal=p.env.owner)


def selected(p,field='英文检索词'):
    return [item for item in detail(p)['suggestions'] if item['field']==field][-1]


def decision(p,item,request='accept-current',**changes):
    asset=current(p)
    changes.setdefault('observation_ids',(p.sought.observation,))
    return FieldDecision(request_id=request,expected_revision=asset.revision,source_sha256=asset.sha256,
        field=item['field'],decision='accept',run_id=item['run_id'],suggestion_id=item['suggestion_id'],
        note='Synthetic explicit per-field review',**changes)


def test_real_precision_ten_field_http_detail_and_exact_adoption(precise):
    p=precise;finish(p)
    client=TestClient(p.sought.app,headers={'Authorization':'Bearer owner'})
    response=client.get('/review/assets/'+current(p).asset_id)
    assert response.status_code==200
    suggestions=response.json()['suggestions']
    assert len(suggestions)==10
    english=next(item for item in suggestions if item['field']=='英文检索词')
    subject=next(item for item in suggestions if item['field']=='画面主体')
    assert english['available'] and english['accept_eligible']
    assert subject['review_state']=='human_effective_value' and not subject['accept_eligible']
    assert client.get(english['evidence'][0]['content']).status_code==200
    spec=decision(p,english)
    accepted=client.post('/review/assets/'+current(p).asset_id+'/fields',json=spec.model_dump(mode='json'))
    assert accepted.status_code==200
    assert p.sought.reviews.decide(current(p).asset_id,spec,principal=p.env.owner)==accepted.json()
    assert any(value.field=='英文检索词' and value.value.values==('synthetic keywords',) for value in current(p).effective_values)
    with pytest.raises(AccessDenied):
        p.sought.reviews.detail(current(p).asset_id,principal=p.env.other)


@pytest.mark.parametrize('state',['queued','failed','cancelled','unreleased'])
def test_latest_field_scope_masks_old_report_for_review_and_evidence(precise,state):
    p=precise;finish(p);old=selected(p)
    deployment=p.deployment();p.submit(deployment,fields=('英文检索词',),request='new-field-scope')
    with p.env.database.transaction(write=True) as connection:
        row=connection.execute("SELECT task_id,job_id FROM router_tasks WHERE json_extract(spec_json,'$.stage')='precision' AND active=1 ORDER BY rowid DESC LIMIT 1").fetchone()
        if state=='failed':connection.execute("UPDATE router_tasks SET state='failed' WHERE task_id=?",(row['task_id'],))
        if state=='cancelled':connection.execute('UPDATE router_jobs SET cancelled=1 WHERE job_id=?',(row['job_id'],))
        if state=='unreleased':connection.execute("UPDATE router_tasks SET state='succeeded' WHERE task_id=?",(row['task_id'],))
    historical=next(item for item in detail(p)['suggestions'] if item['suggestion_id']==old['suggestion_id'])
    assert not historical['available'] and not historical['accept_eligible']
    with pytest.raises(RevisionConflict):
        p.sought.reviews.decide(current(p).asset_id,decision(p,old),principal=p.env.owner)
    # Other fields in the old run remain eligible, including their shared input
    # evidence even if one suggestion in that run is now superseded.
    chinese=selected(p,'中文检索词')
    if state=='cancelled':
        assert not chinese['available']  # Both scopes belong to the cancelled job.
        return
    assert chinese['available']
    client=TestClient(p.sought.app,headers={'Authorization':'Bearer owner'})
    assert client.get(chinese['evidence'][0]['content']).status_code==200


def test_precision_probe_integrity_and_saved_suggestion_are_reverified(precise):
    p=precise;finish(p);item=selected(p)
    with p.env.database.transaction() as connection:
        run=connection.execute('SELECT task_json FROM suggestion_runs WHERE run_id=?',(item['run_id'],)).fetchone()
    probe=next(ref for ref in json.loads(run[0])['input_refs'] if ref['kind']=='technical_report')
    path=p.env.workspace/probe['ref'];path.write_bytes(path.read_bytes()+b' ')
    assert not selected(p)['available']
    with pytest.raises(RevisionConflict,match='evidence is unavailable'):
        p.sought.reviews.decide(current(p).asset_id,decision(p,item),principal=p.env.owner)
    client=TestClient(p.sought.app,headers={'Authorization':'Bearer owner'})
    assert client.post('/review/assets/'+current(p).asset_id+'/fields',json=decision(p,item).model_dump(mode='json')).status_code==409


def test_human_unknown_and_rejection_require_explicit_manual_revision(precise):
    p=precise;finish(p);item=selected(p)
    asset=current(p)
    p.sought.reviews.decide(asset.asset_id,FieldDecision(request_id='human-unknown',expected_revision=asset.revision,
        source_sha256=asset.sha256,field='英文检索词',value=FieldValue(state='unknown',reason='Human reserves the value'),
        observation_ids=(p.sought.observation,),
        note='Synthetic manual unknown'),principal=p.env.owner)
    assert selected(p)['review_state']=='human_effective_value'
    with pytest.raises(RevisionConflict,match='human_effective_value'):
        p.sought.reviews.decide(current(p).asset_id,decision(p,item),principal=p.env.owner)
    chinese=selected(p,'中文检索词');asset=current(p)
    rejected=decision(p,chinese,'reject-it').model_copy(update={'decision':'reject'})
    p.sought.reviews.decide(asset.asset_id,rejected,principal=p.env.owner)
    assert selected(p,'中文检索词')['review_state']=='human_rejected_suggestion'
    with pytest.raises(RevisionConflict,match='human_rejected_suggestion'):
        p.sought.reviews.decide(current(p).asset_id,decision(p,chinese,'reaccept'),principal=p.env.owner)
    asset=current(p)
    p.sought.reviews.decide(asset.asset_id,FieldDecision(request_id='explicit-manual',expected_revision=asset.revision,
        source_sha256=asset.sha256,field='英文检索词',value=FieldValue(state='observed',values=('human keyword',)),
        observation_ids=(p.sought.observation,),
        note='Synthetic explicit manual revision'),principal=p.env.owner)
    assert any(value.field=='英文检索词' and value.value.values==('human keyword',) for value in current(p).effective_values)


def test_actual_medical_observation_and_continuous_direction_gate_remain_required(precise):
    p=precise;deployment=p.deployment()
    p.bridge.write_text(p.bridge.read_text(encoding='utf8').replace(
        "('科室', '器官/系统', '疾病/症状', '过程/状态', '情绪/语气')", "('情绪/语气',)"),encoding='utf8')
    provider=deployment.providers[0].model_copy(update={'adapter_sha256':sha256(p.bridge.read_bytes()).hexdigest()})
    deployment=deployment.model_copy(update={'providers':(provider,)})
    p.analysis.precision_deployment=deployment
    _,_,executor=p.submit(deployment,fields=('科室',));assert executor.run_once()['accepted']
    item=selected(p,'科室')
    with pytest.raises(ValueError,match='observations required'):
        p.sought.reviews.decide(current(p).asset_id,decision(p,item,observation_ids=()),principal=p.env.owner)
    with pytest.raises(ValueError,match='continuous video'):
        p.sought.reviews.decide(current(p).asset_id,decision(p,item,observation_ids=(p.sought.observation,),dynamic=True),principal=p.env.owner)
    p.sought.reviews.decide(current(p).asset_id,decision(p,item,observation_ids=(p.sought.observation,)),principal=p.env.owner)
    assert any(value.field=='科室' for value in current(p).effective_values)


def test_local_topics_shared_verifier_and_explicit_protocol_mock(topic_router,monkeypatch):
    import media_tag.local_topics.projection as local
    t=topic_router
    # Existing isolated protocol mock advertises local_qwen wire identity. It
    # never loads a model and does not alter production synthetic rejection.
    assert t.executor.run_once()['accepted']
    actual=local.verified;calls=[]
    def checked(*args):
        calls.append(True);return actual(*args)
    monkeypatch.setattr(local,'verified',checked)
    rows=t.sought.reviews.detail(t.sought.asset.asset_id,principal=t.env.owner)['suggestions']
    english=next(item for item in rows if item['field']=='英文检索词')
    assert english['available'] and calls
    asset=t.env.catalog.get_asset(t.sought.asset.asset_id,principal=t.env.owner)
    t.sought.reviews.decide(asset.asset_id,FieldDecision(request_id='local-adopt',expected_revision=asset.revision,
        source_sha256=asset.sha256,field='英文检索词',decision='accept',run_id=english['run_id'],
        suggestion_id=english['suggestion_id'],observation_ids=(t.sought.observation,),
        note='Synthetic protocol-only local topic review'),principal=t.env.owner)
    assert calls


def test_different_model_generation_masks_old_active_field_suggestion(precise):
    p=precise;finish(p);old=selected(p)
    deployment=p.deployment()
    provider=deployment.providers[0].model_copy(update={'model_revision':'protocol-only-v2'})
    deployment=deployment.model_copy(update={'providers':(provider,)})
    p.analysis.precision_deployment=deployment
    _,_,executor=p.submit(deployment,fields=('英文检索词',),request='different-model-generation')
    assert executor.run_once()['accepted']
    items=[item for item in detail(p)['suggestions'] if item['field']=='英文检索词']
    historical=next(item for item in items if item['suggestion_id']==old['suggestion_id'])
    newest=next(item for item in items if item['suggestion_id']!=old['suggestion_id'])
    assert not historical['available'] and newest['available']
    with pytest.raises(RevisionConflict):
        p.sought.reviews.decide(current(p).asset_id,decision(p,old),principal=p.env.owner)


def test_evidence_verification_is_readonly_and_concurrent_producer_change_fences_accept(precise,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import media_tag.review.qualification as qualification
    p=precise;finish(p);item=selected(p);spec=decision(p,item)
    entered=threading.Event();written=threading.Event();actual=qualification.verify
    def slow(*args):
        entered.set();assert written.wait(5)
        return actual(*args)
    monkeypatch.setattr(qualification,'verify',slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(p.sought.reviews.decide,current(p).asset_id,spec,principal=p.env.owner)
        assert entered.wait(5)
        try:
            with p.env.database.transaction(write=True) as connection:
                connection.execute("UPDATE router_jobs SET paused=1 WHERE job_id IN (SELECT t.job_id FROM router_tasks t JOIN suggestion_runs r ON json_extract(r.task_json,'$.task_id')=t.task_id WHERE r.run_id=?)",(item['run_id'],))
        finally:written.set()
        with pytest.raises(RevisionConflict):pending.result()
    assert not any(value.field=='英文检索词' for value in current(p).effective_values)


def test_consistently_rehashed_synthetic_local_report_still_cannot_be_reviewed(topic_router):
    from local_topics.test_router import rewrite_report
    t=topic_router;assert t.executor.run_once()['accepted']
    items=t.sought.reviews.detail(t.sought.asset.asset_id,principal=t.env.owner)['suggestions']
    english=next(item for item in items if item['field']=='英文检索词')
    row,_,_=t.rows()
    row=rewrite_report(t,row,lambda report,result:report['result'].update(backend='synthetic_protocol'))
    # Keep the immutable-run mirror consistent in this corruption fixture so
    # rejection really reaches the shared production semantic verifier.
    with t.env.database.transaction(write=True) as connection:
        connection.execute('UPDATE suggestion_runs SET result_json=? WHERE run_id=?',(row['result_json'],english['run_id']))
    rows=t.sought.reviews.detail(t.sought.asset.asset_id,principal=t.env.owner)['suggestions']
    assert not any(item['available'] for item in rows)
    asset=t.env.catalog.get_asset(t.sought.asset.asset_id,principal=t.env.owner)
    with pytest.raises(ValueError,match='report_binding'):
        t.sought.reviews.decide(asset.asset_id,FieldDecision(request_id='reject-synthetic',expected_revision=asset.revision,
            source_sha256=asset.sha256,field='英文检索词',decision='accept',run_id=english['run_id'],
            suggestion_id=english['suggestion_id'],observation_ids=(t.sought.observation,),
            note='Synthetic payload must never become reviewed production data'),principal=t.env.owner)


def test_disabled_stage_cannot_expose_old_suggestion_as_available(precise):
    p=precise;finish(p);item=selected(p)
    del p.env.router.stages['precision']
    assert not selected(p)['available']
    with pytest.raises(RevisionConflict,match='no longer configured'):
        p.sought.reviews.decide(current(p).asset_id,decision(p,item),principal=p.env.owner)


def test_detail_eligibility_includes_actual_medical_reviewer_binding(precise):
    from media_tag.review.service import ReviewService
    p=precise;finish(p,fields=('科室',))
    service=ReviewService(p.env.router,p.sought.reviews.config.model_copy(update={'reviewers':()}))
    item=service.detail(current(p).asset_id,principal=p.env.owner)['suggestions'][0]
    assert item['available'] and not item['accept_eligible']
    assert item['review_state']=='medical_reviewer_required' and item['requires_medical_reviewer']
    with pytest.raises(AccessDenied,match='medical reviewer'):
        service.decide(current(p).asset_id,decision(p,item),principal=p.env.owner)


def test_evidence_response_rechecks_producer_after_actual_bytes_are_read(precise,monkeypatch):
    from contextlib import contextmanager
    p=precise;finish(p);item=selected(p);url=item['evidence'][0]['content']
    actual=p.env.router.files.open_read;changed=[]
    @contextmanager
    def raced(ref):
        with actual(ref) as stream:
            if not changed and ref.endswith('input.bin'):
                with p.env.database.transaction(write=True) as connection:
                    connection.execute("UPDATE router_jobs SET paused=1 WHERE job_id IN (SELECT job_id FROM router_tasks WHERE task_id=(SELECT json_extract(task_json,'$.task_id') FROM suggestion_runs WHERE run_id=?))",(item['run_id'],))
                changed.append(True)
            yield stream
    monkeypatch.setattr(p.env.router.files,'open_read',raced)
    client=TestClient(p.sought.app,headers={'Authorization':'Bearer owner'})
    response=client.get(url)
    assert changed and response.status_code==409
