import csv
from hashlib import sha256
from io import StringIO
import json

import pytest

from media_tag.drafts.service import DraftIndex
from media_tag.export.models import AssetSelection, ExportSubmission
from search.test_query import query


def finish(p, *, fields=None, mode='normal', request='precision-one'):
    deployment=p.deployment(mode)
    _,receipt,executor=p.submit(deployment,fields=fields,request=request)
    result=executor.run_once()
    assert result['accepted'], result
    return receipt


def projected(p, *, persist=False):
    with p.env.database.transaction(write=persist) as connection:
        asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(p.sought.asset.asset_id,)).fetchone()
        return DraftIndex(p.env.router).sync(connection,asset,persist=persist)


def exported(p, request='csv', **options):
    asset=p.env.catalog.get_asset(p.sought.asset.asset_id,principal=p.env.owner)
    submission=ExportSubmission(request_id=request,assets=(AssetSelection(asset_id=asset.asset_id,
        expected_revision=asset.revision,source_sha256=asset.sha256),),**options)
    service=p.sought.app.state.exports
    receipt=service.create(submission,principal=p.env.owner)
    data=service.content(receipt['export_id'],'assets.csv',principal=p.env.owner)
    rows=list(csv.DictReader(StringIO(data.decode('utf-8-sig'))))
    return rows[0],service,receipt


def test_real_report_is_searchable_but_human_values_win(precise):
    p=precise
    finish(p)
    draft=projected(p,persist=True)
    labels=[label for label in draft['labels'] if label.get('origin')=='precision']
    assert labels and not any(label['field']=='画面主体' for label in labels)
    assert draft['field_states']['画面主体']['state']=='observed'
    assert all(label['medical_confirmation'] is False for label in labels)
    assert query(p.sought,'synthetic keywords')['items']==[]
    found=query(p.sought,'synthetic keywords',include_drafts=True)
    assert found['items']
    reasons=found['items'][0]['versions'][0]['reasons']
    assert any(reason.get('origin')=='precision' and reason['medical_evidence'] is False for reason in reasons)
    public=json.dumps(found,ensure_ascii=False)
    assert str(p.env.workspace) not in public and str(p.bridge) not in public
    assert not query(p.sought,'synthetic keywords',include_drafts=True,actor=p.env.other)['items']


def test_csv_uses_current_six_nonmedical_drafts_and_preserves_35_headers(precise):
    p=precise;finish(p)
    default,_,_=exported(p)
    assert default['英文检索词']==''
    result,_,_=exported(p,'csv-ai',include_ai_drafts=True)
    assert len(result)==35 and result['画面主体']=='合成圆形'
    assert result['英文检索词']=='synthetic keywords' and result['标签状态']=='AI初稿'
    assert result['许可状态']=='未核实'
    assert all(result[field]=='' for field in ('科室','器官/系统','疾病/症状','过程/状态'))


@pytest.mark.parametrize('mode,expected',[('unknown','unknown'),('outside','no_match')])
def test_unresolved_new_scope_does_not_reuse_success(precise,mode,expected):
    p=precise;finish(p)
    field='用途大类' if mode=='outside' else '英文检索词'
    finish(p,fields=(field,),mode=mode,request='precision-two')
    draft=projected(p)
    assert draft['field_states'][field]['state']==expected
    assert not any(label.get('field')==field for label in draft['labels'])
    assert any(label.get('field')=='中文检索词' for label in draft['labels'])
    result,_,_=exported(p,'unresolved',include_ai_drafts=True)
    assert result[field]=='' and result['中文检索词']


def test_pending_failed_and_cancelled_latest_scope_mask_older_success(precise):
    p=precise;finish(p)
    deploy=p.deployment()
    _,receipt,_=p.submit(deploy,fields=('英文检索词',),request='precision-two')
    for state in ('pending','failed','cancelled'):
        with p.env.database.transaction(write=True) as connection:
            row=connection.execute("SELECT t.task_id,t.job_id FROM router_tasks t WHERE active=1 AND json_extract(spec_json,'$.stage')='precision' ORDER BY rowid DESC LIMIT 1").fetchone()
            if state=='failed':connection.execute("UPDATE router_tasks SET state='failed' WHERE task_id=?",(row['task_id'],))
            if state=='cancelled':connection.execute('UPDATE router_jobs SET cancelled=1 WHERE job_id=?',(row['job_id'],))
        draft=projected(p)
        assert not any(label.get('field')=='英文检索词' for label in draft['labels']),state


def test_cached_nonmatch_skips_io_but_returned_candidate_revalidates(precise,monkeypatch):
    import media_tag.topic_index.service as index
    p=precise;finish(p);projected(p,persist=True)
    actual=index.verified;calls=[]
    def checked(*args):
        calls.append(True);return actual(*args)
    monkeypatch.setattr(index,'verified',checked)
    assert query(p.sought,'unfindable-token',include_drafts=True)['items']==[]
    assert calls==[]
    assert query(p.sought,'synthetic keywords',include_drafts=True)['items']
    assert calls
    with p.env.database.transaction() as connection:
        report=connection.execute("SELECT payload_json FROM ai_draft_documents WHERE stage='precision'").fetchone()
        document=json.loads(report[0]);digest=document['provenance']['英文检索词']['report_sha256']
        result=connection.execute("SELECT result_json FROM router_attempts WHERE result_json LIKE ?",('%'+digest+'%',)).fetchone()
        ref=next(item['ref'] for item in json.loads(result[0])['artifacts'] if item['sha256']==digest)
    target=p.env.workspace/ref;target.write_bytes(target.read_bytes()+b' ')
    assert query(p.sought,'synthetic keywords',include_drafts=True)['items']==[]


def test_observed_medical_draft_is_only_quality_sidecar_and_search_suggestion(precise):
    p=precise;deployment=p.deployment()
    p.bridge.write_text(p.bridge.read_text(encoding='utf8').replace(
        "('科室', '器官/系统', '疾病/症状', '过程/状态', '情绪/语气')", "('情绪/语气',)"),encoding='utf8')
    provider=deployment.providers[0].model_copy(update={'adapter_sha256':sha256(p.bridge.read_bytes()).hexdigest()})
    deployment=deployment.model_copy(update={'providers':(provider,)})
    p.analysis.precision_deployment=deployment
    _,_,executor=p.submit(deployment,fields=('科室','疾病/症状'))
    assert executor.run_once()['accepted']
    draft=projected(p)
    assert draft['field_states']['科室']['values']==['合成科室-仅测试']
    result,service,receipt=exported(p,include_ai_drafts=True)
    assert result['科室']=='' and result['疾病/症状']==''
    quality=json.loads(service.content(receipt['export_id'],'quality.json',principal=p.env.owner))
    medical=[issue for issue in quality['issues'] if issue['code']=='unconfirmed_medical_ai_suggestion']
    assert len(medical)==2 and all(issue['suggested_values'] for issue in medical)
    found=query(p.sought,'合成科室-仅测试',include_drafts=True)
    assert found['items']
    assert all(not reason['medical_evidence'] for reason in found['items'][0]['versions'][0]['reasons'])
    with p.env.database.transaction() as connection:
        assert not connection.execute("SELECT 1 FROM effective_values WHERE field IN ('科室','疾病/症状')").fetchone()


def test_precision_verification_during_search_does_not_hold_writer_lock(precise,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import media_tag.topic_index.service as index
    p=precise;finish(p);entered=threading.Event();written=threading.Event()
    actual=index.verified
    def slow(*args):
        entered.set();assert written.wait(5)
        return actual(*args)
    monkeypatch.setattr(index,'verified',slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(query,p.sought,'synthetic keywords',include_drafts=True)
        assert entered.wait(5)
        try:
            with p.env.database.transaction(write=True) as connection:
                connection.execute("INSERT INTO catalog_metadata VALUES('topic-concurrent-worker','ok')")
        finally:written.set()
        assert pending.result()['items']


def test_new_precision_scope_fences_export_snapshot_without_media_hash_under_writer(precise):
    from media_tag.catalog import RevisionConflict
    p=precise;finish(p)
    asset=p.env.catalog.get_asset(p.sought.asset.asset_id,principal=p.env.owner)
    spec=ExportSubmission(request_id='snapshot-fence',assets=(AssetSelection(asset_id=asset.asset_id,
        expected_revision=asset.revision,source_sha256=asset.sha256),),include_ai_drafts=True)
    service=p.sought.app.state.exports;snapshot=service._snapshot(spec,p.env.owner)
    deployment=p.deployment();p.submit(deployment,fields=('英文检索词',),request='new-field-scope')
    with p.env.database.transaction(write=True) as connection:
        with pytest.raises(RevisionConflict,match='precision producer changed'):
            service._current(connection,snapshot,p.env.owner)


def test_explicit_human_unknown_takes_priority_over_precision_and_cached_projection(precise):
    from media_tag.contracts.models import FieldValue
    from media_tag.review.models import FieldDecision
    p=precise;finish(p);projected(p,persist=True)
    asset=p.env.catalog.get_asset(p.sought.asset.asset_id,principal=p.env.owner)
    p.sought.reviews.decide(asset.asset_id,FieldDecision(request_id='human-unknown',
        expected_revision=asset.revision,source_sha256=asset.sha256,field='英文检索词',
        value=FieldValue(state='unknown',reason='Human explicitly reserves this field'),
        observation_ids=(p.sought.observation,),note='Synthetic human decision overrides precision'),principal=p.env.owner)
    assert not any(label.get('field')=='英文检索词' for label in projected(p)['labels'])
    assert not query(p.sought,'synthetic keywords',include_drafts=True)['items']
    result,_,_=exported(p,include_ai_drafts=True)
    assert result['英文检索词']==''


def test_rejected_precision_field_and_current_privacy_gate_remove_search_results(precise,monkeypatch):
    from media_tag.review.models import FieldDecision
    p=precise;finish(p);draft=projected(p,persist=True)
    provenance=draft['topic_field_provenance']['英文检索词']
    asset=p.env.catalog.get_asset(p.sought.asset.asset_id,principal=p.env.owner)
    p.sought.reviews.decide(asset.asset_id,FieldDecision(request_id='reject-precision',
        expected_revision=asset.revision,source_sha256=asset.sha256,field='英文检索词',decision='reject',
        run_id=provenance['run_id'],suggestion_id=provenance['suggestion_id'],
        note='Synthetic reviewer rejection of model suggestion'),principal=p.env.owner)
    assert not query(p.sought,'synthetic keywords',include_drafts=True)['items']
    assert projected(p)['field_states']['英文检索词']['reason']=='human_rejected_suggestion'
    assert query(p.sought,'合成精标圆形',include_drafts=True)['items']
    # The search service rechecks the current privacy gate before selecting a
    # cached projection. This is an authorization boundary, not a model fixture.
    monkeypatch.setattr(p.sought.search.files,'_gate',lambda *args:'synthetic_pending_privacy_review')
    assert not query(p.sought,'合成精标圆形',include_drafts=True)['items']


def test_private_adapter_path_in_actual_model_value_is_suppressed(precise):
    p=precise;deployment=p.deployment()
    p.bridge.write_text(p.bridge.read_text(encoding='utf8').replace("'synthetic keywords' if name == '英文检索词'",
        repr(str(p.bridge))+" if name == '英文检索词'"),encoding='utf8')
    provider=deployment.providers[0].model_copy(update={'adapter_sha256':sha256(p.bridge.read_bytes()).hexdigest()})
    deployment=deployment.model_copy(update={'providers':(provider,)})
    p.analysis.precision_deployment=deployment
    _,_,executor=p.submit(deployment,fields=('英文检索词',))
    assert executor.run_once()['accepted']
    draft=projected(p)
    assert draft['field_states']['英文检索词']['reason']=='private_value_suppressed'
    result,service,receipt=exported(p,include_ai_drafts=True)
    assert result['英文检索词']==''
    quality=service.content(receipt['export_id'],'quality.json',principal=p.env.owner).decode('utf8')
    assert str(p.bridge) not in quality and str(p.env.workspace) not in quality


def test_producer_version_change_invalidates_current_cached_projection(precise):
    p=precise;finish(p);projected(p,persist=True)
    with p.env.database.transaction(write=True) as connection:
        row=connection.execute("SELECT a.task_id,a.lease_epoch,a.envelope_json FROM router_attempts a JOIN router_tasks t USING(task_id) WHERE json_extract(t.spec_json,'$.stage')='precision' ORDER BY t.rowid DESC LIMIT 1").fetchone()
        envelope=json.loads(row['envelope_json']);envelope['versions']['model_revision']='changed-producer-version'
        connection.execute('UPDATE router_attempts SET envelope_json=? WHERE task_id=? AND lease_epoch=?',
            (json.dumps(envelope),row['task_id'],row['lease_epoch']))
    assert not query(p.sought,'synthetic keywords',include_drafts=True)['items']
    result,_,_=exported(p,include_ai_drafts=True)
    assert result['英文检索词']==''


def test_modified_probe_removes_precision_candidate_without_search_error(precise):
    p=precise;finish(p);projected(p,persist=True)
    with p.env.database.transaction() as connection:
        row=connection.execute("SELECT a.envelope_json FROM router_attempts a JOIN router_tasks t USING(task_id) WHERE json_extract(t.spec_json,'$.stage')='precision' ORDER BY t.rowid DESC LIMIT 1").fetchone()
        envelope=json.loads(row['envelope_json'])
    ref=next(ref for ref in envelope['input_refs'] if ref['kind']=='technical_report')
    (p.env.workspace/ref['ref']).write_text('{}')
    assert not query(p.sought,'synthetic keywords',include_drafts=True)['items']
    # The existing technical export gate rejects a corrupt probe entirely.
    with pytest.raises(ValueError,match='hash mismatch'):
        exported(p,include_ai_drafts=True)
