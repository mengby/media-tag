"""Actual isolated catalogue/ASGI HTTP/worker reports, synthetic bridge only."""
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
import wave

import pytest
from fastapi.testclient import TestClient
from precision.conftest import precise,sought,env,tools
from media_tag.analysis.projection import TopicProjection
from media_tag.analysis.intake import ensure_intent
from media_tag.api.app import create_app
from media_tag.catalog import AccessDenied
from media_tag.files.service import FileError,FileService
from media_tag.precision.runtime import PrecisionHandlers
from media_tag.precision.service import PrecisionSubmission,request_precision
from media_tag.router.runtime import CPUHandlers,LocalExecutor
from media_tag.topic_evaluation import Dataset,Export,evaluate,freeze_catalog_export,FreezeOptions
from media_tag.topic_evaluation.io import EvaluationError


@pytest.fixture
def catalog_freeze(precise,tmp_path):
    p=precise;e=p.env;deploy=p.deployment()
    spec,accepted,executor=p.submit(deploy);assert executor.run_once()['accepted']
    asset_id=p.sought.asset.asset_id
    with e.database.transaction() as connection:
        asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
        intent=connection.execute('SELECT * FROM analysis_intents WHERE asset_id=? AND requested_by=?',(asset_id,e.owner.actor_id)).fetchone()
        binding=connection.execute('SELECT job_id FROM router_requests WHERE owner_id=? AND request_id=?',(e.owner.actor_id,intent['intent_id'])).fetchone()
        job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(binding['job_id'],)).fetchone()
        document=TopicProjection(e.router).build(connection,asset,'image',job)[0]
    folder=tmp_path/'frozen-inputs';folder.mkdir();(folder/'README.md').write_text('Synthetic catalogue export dataset only.')
    output=tmp_path/'private-freeze';output.mkdir()
    def dataset_for(*,asset_id=asset_id,source_sha=document.content_sha256,kind='image',topic_document=None):
        topic_document=topic_document or document
        gold=[]
        for field in topic_document.fields:
            na=(kind=='audio' and field.field in ('画面主体','表现形式')) or field.value.state=='not_applicable'
            gold_state='not_applicable' if na else 'labelled' if field.value.state=='observed' else 'unassessable'
            gold.append({'field':field.field,'state':gold_state,'complete_value_set':gold_state=='labelled',
                'values':[] if na else list(field.value.values or ()),'annotation_note':'Synthetic protocol expectation; no real human/model accuracy claim.'})
        dataset=Dataset.model_validate_json(json.dumps({'dataset_id':'synthetic-current-catalogue','purpose':'synthetic_engineering',
            'frozen_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),
            'annotators':['synthetic-annotator'],'adjudicator':'synthetic-adjudicator','independently_human_annotated':False,
            'annotation_reference':'Synthetic bridge protocol, not independent real business labels.',
            'taxonomy_version':p.sought.reviews.policy.config.taxonomy_version,
            'taxonomy_sha256':p.sought.reviews.policy.config.taxonomy_manifest_sha256,
            'runs':[{'run_id':'synthetic-current','producer_pins':[{'versions':producer.versions.model_dump(mode='json'),
                'prompt_version':producer.prompt_version} for producer in topic_document.producers]}],
            'cases':[{'case_id':'synthetic-case','source_sha256':source_sha,'content_kind':kind,'near_group':'synthetic-family',
                'split':'acceptance','annotation_reference':'Synthetic actual bytes only.','gold':gold}]}))
        path=folder/'dataset.json';path.write_text(dataset.model_dump_json(),encoding='utf8')
        return path,{'synthetic-case':asset_id}
    path,mapping=dataset_for()
    def freeze(*,principal=e.owner,options=None,private_output=output):
        return freeze_catalog_export(e.router,path,mapping,'synthetic-current',principal=principal,
            private_output=private_output,options=options)
    app=create_app(e.router,{sha256(name.encode()).hexdigest():person for name,person in
        [('owner',e.owner),('other',e.other),('worker',e.worker)]},analysis_options=p.analysis.options,precision_options=deploy)
    return SimpleNamespace(p=p,e=e,path=path,mapping=mapping,freeze=freeze,output=output,
        document=document,accepted=accepted,executor=executor,dataset_for=dataset_for,client=TestClient(app))


def other_actor_analysis(f):
    asset_id=f.mapping['synthetic-case'];e=f.e;p=f.p
    asset=e.catalog.get_asset(asset_id,principal=e.owner)
    e.catalog.change_access(asset_id,principal=e.owner,expected_revision=asset.revision,
        subject_id=e.other.actor_id,permission='edit',reason='Synthetic test grants explicit analysis scope.')
    with e.database.transaction(write=True) as connection:
        ensure_intent(connection,asset_id,e.other.actor_id,content_kind='image')
    p.analysis.dispatch_pending(principal=e.other)
    cpu=LocalExecutor(e.router,e.worker,CPUHandlers(e.router,e.worker,p.tools))
    for _ in range(12):
        if cpu.run_once() is None:break
    state=p.analysis.status(asset_id,principal=e.other);asset=e.catalog.get_asset(asset_id,principal=e.other)
    deployment=p.analysis.precision_deployment
    request=PrecisionSubmission(request_id='synthetic-other-run',expected_asset_revision=asset.revision,
        source_sha256=asset.sha256,expected_job_revision=state['runtime']['revision'])
    request_precision(p.analysis,deployment,asset_id,request,principal=e.other)
    assert LocalExecutor(e.router,e.worker,PrecisionHandlers(e.router,e.worker,deployment)).run_once()['accepted']
    for permission,revoke in (('read',False),('edit',True)):
        asset=e.catalog.get_asset(asset_id,principal=e.owner)
        e.catalog.change_access(asset_id,principal=e.owner,expected_revision=asset.revision,
            subject_id=e.other.actor_id,permission=permission,revoke=revoke,reason='Synthetic read-only export scope.')


def test_actual_http_current_projection_to_private_bundle_and_evaluation(catalog_freeze):
    f=catalog_freeze
    response=f.client.get('/analysis/assets/'+f.mapping['synthetic-case'],headers={'Authorization':'Bearer owner'})
    assert response.status_code==200 and response.json()['topic_analysis']['analysis_complete']
    assert 'jobs/' not in response.text and 'adapter_path' not in response.text
    before=f.e.catalog.get_asset(f.mapping['synthetic-case'],principal=f.e.owner)
    with f.e.database.transaction() as connection:before_database=tuple(connection.iterdump())
    result=f.freeze()
    exported=Export.model_validate_json(result['export_path'].read_bytes())
    assert exported.predictions[0].timing_scope=='sum_verified_producer_stage_seconds'
    assert exported.predictions[0].document is not None
    evaluation=evaluate(result['dataset_path'],result['export_path'])['report']
    assert evaluation['overall']['exact_match_labelled']['numerator']==sum(field.value.state=='observed' for field in f.document.fields)
    assert evaluation['overall']['structural_assessment_success']['value']==1.
    assert not evaluation['business_acceptance'] and not evaluation['calibrated']
    after=f.e.catalog.get_asset(before.asset_id,principal=f.e.owner)
    assert after==before
    with f.e.database.transaction() as connection:assert tuple(connection.iterdump())==before_database
    receipt=json.loads(result['receipt_path'].read_text(encoding='utf8'))
    assert receipt['source_reports_copied_byte_exact']
    assert receipt['prediction_export']['sha256']==sha256(result['export_path'].read_bytes()).hexdigest()
    redirects=receipt['cases'][0]['artifact_redirects']
    assert all(sha256((result['directory']/item['bundle_ref']).read_bytes()).hexdigest()==item['original_artifact_sha256'] for item in redirects)
    for producer in f.document.producers:
        original=(f.e.workspace/producer.report.ref).read_bytes()
        copied=next(item['bundle_ref'] for item in redirects if item['original_artifact_sha256']==producer.report.sha256)
        assert (result['directory']/copied).read_bytes()==original


def test_actor_read_and_worker_role_denied(catalog_freeze):
    f=catalog_freeze
    assert f.client.get('/analysis/assets/'+f.mapping['synthetic-case'],headers={'Authorization':'Bearer other'}).status_code==403
    with pytest.raises((FileError,AccessDenied)):f.freeze(principal=f.e.other)
    with pytest.raises(AccessDenied):f.freeze(principal=f.e.worker)
    assert not list(f.output.iterdir())


def test_source_hash_must_equal_explicit_dataset_case(catalog_freeze):
    f=catalog_freeze;f.dataset_for(source_sha='b'*64)
    with pytest.raises(FileError) as error:f.freeze()
    assert error.value.status==412
    assert not list(f.output.iterdir())


def test_unreleased_current_task_exports_failure_without_old_success(catalog_freeze):
    f=catalog_freeze
    with f.e.database.transaction(write=True) as connection:
        connection.execute('UPDATE router_attempts SET released_at=NULL WHERE task_id=?',(f.accepted['task_id'],))
    result=f.freeze();exported=Export.model_validate_json(result['export_path'].read_bytes())
    assert exported.predictions[0].document is None and exported.predictions[0].elapsed_seconds is None
    assert exported.predictions[0].timing_scope=='not_measured'
    assert evaluate(result['dataset_path'],result['export_path'])['report']['overall']['failures']==10


def test_damaged_report_does_not_export_a_stale_document(catalog_freeze):
    f=catalog_freeze
    (f.e.workspace/f.document.producers[0].report.ref).write_bytes(b'corrupted private synthetic report')
    result=f.freeze();exported=Export.model_validate_json(result['export_path'].read_bytes())
    assert exported.predictions[0].failure_code=='current_topic_evidence_unavailable'
    assert exported.predictions[0].document is None


def test_metadata_change_after_long_copy_aborts_and_discards_staging(catalog_freeze,monkeypatch):
    f=catalog_freeze
    import media_tag.topic_evaluation.exporter as module
    original=module._Writer.copy;changed=False
    def copy(self,*args):
        nonlocal changed
        result=original(self,*args)
        if not changed:
            changed=True
            with f.e.database.transaction(write=True) as connection:
                connection.execute('UPDATE assets SET revision=revision+1,name=? WHERE asset_id=?',
                    ('Synthetic metadata changed mid-export',f.mapping['synthetic-case']))
        return result
    monkeypatch.setattr(module._Writer,'copy',copy)
    with pytest.raises(EvaluationError,match='metadata_changed_during_read'):f.freeze()
    assert not list(f.output.iterdir())


def test_current_file_gate_is_rechecked(catalog_freeze,monkeypatch):
    f=catalog_freeze
    original=FileService._gate
    def gated(self,connection,asset,**kwargs):
        return 'privacy_review_pending' if asset['asset_id']==f.mapping['synthetic-case'] else original(self,connection,asset,**kwargs)
    monkeypatch.setattr(FileService,'_gate',gated)
    with pytest.raises(FileError) as error:f.freeze()
    assert error.value.code=='privacy_review_pending'
    assert not list(f.output.iterdir())


def test_explicit_mapping_and_output_isolation_required(catalog_freeze):
    f=catalog_freeze
    with pytest.raises(EvaluationError,match='mapping_required'):
        freeze_catalog_export(f.e.router,f.path,{},'synthetic-current',principal=f.e.owner,private_output=f.output)
    with pytest.raises(EvaluationError,match='private_and_disjoint'):f.freeze(private_output=f.e.workspace)


def test_budget_failure_does_not_publish_partial_bundle(catalog_freeze):
    f=catalog_freeze
    with pytest.raises(EvaluationError,match='budget'):f.freeze(options=FreezeOptions(max_file_bytes=512))
    assert not list(f.output.iterdir())


def test_read_only_actor_can_export_own_existing_analysis(catalog_freeze):
    f=catalog_freeze;other_actor_analysis(f)
    result=f.freeze(principal=f.e.other)
    exported=Export.model_validate_json(result['export_path'].read_bytes())
    assert exported.predictions[0].document is not None
    assert json.loads(result['receipt_path'].read_text(encoding='utf8'))['actor_id']==f.e.other.actor_id


def test_actual_read_revocation_during_copy_aborts_private_publication(catalog_freeze,monkeypatch):
    f=catalog_freeze;other_actor_analysis(f)
    import media_tag.topic_evaluation.exporter as module
    original=module._Writer.copy;changed=False
    def copy(self,*args):
        nonlocal changed
        result=original(self,*args)
        if not changed:
            changed=True;asset=f.e.catalog.get_asset(f.mapping['synthetic-case'],principal=f.e.owner)
            f.e.catalog.change_access(asset.asset_id,principal=f.e.owner,expected_revision=asset.revision,
                subject_id=f.e.other.actor_id,permission='read',revoke=True,reason='Synthetic revocation during bounded export.')
        return result
    monkeypatch.setattr(module._Writer,'copy',copy)
    with pytest.raises(FileError) as error:f.freeze(principal=f.e.other)
    assert error.value.status==403 and not list(f.output.iterdir())
    response=f.client.get('/analysis/assets/'+f.mapping['synthetic-case'],headers={'Authorization':'Bearer other'})
    assert response.status_code==403


def test_reader_cannot_borrow_owners_current_analysis(catalog_freeze):
    f=catalog_freeze;asset=f.e.catalog.get_asset(f.mapping['synthetic-case'],principal=f.e.owner)
    f.e.catalog.change_access(asset.asset_id,principal=f.e.owner,expected_revision=asset.revision,
        subject_id=f.e.other.actor_id,permission='read',reason='Synthetic object read only, not another actor job.')
    result=f.freeze(principal=f.e.other)
    exported=Export.model_validate_json(result['export_path'].read_bytes())
    assert exported.predictions[0].failure_code=='current_actor_topic_projection_not_requested'
    assert exported.predictions[0].document is None and exported.predictions[0].elapsed_seconds is None


def test_current_pending_field_shadows_older_success_in_frozen_document(catalog_freeze):
    f=catalog_freeze
    f.p.submit(f.p.analysis.precision_deployment,fields=('情绪/语气',),request='new-current-pending-field')
    result=f.freeze();exported=Export.model_validate_json(result['export_path'].read_bytes())
    doc=json.loads((result['directory']/exported.predictions[0].document.ref).read_text(encoding='utf8'))
    field=next(item for item in doc['fields'] if item['field']=='情绪/语气')
    assert field['value']['state']=='unknown' and not field['producer_ids']
    assert evaluate(result['dataset_path'],result['export_path'])['report']['overall']['structural_assessment_success']['numerator']==9


def test_policy_change_during_copy_does_not_publish_old_qualified_values(catalog_freeze,monkeypatch):
    f=catalog_freeze
    import media_tag.topic_evaluation.exporter as module
    original=module._Writer.copy;changed=False
    def copy(self,*args):
        nonlocal changed
        result=original(self,*args)
        if not changed:changed=True;f.p.sought.reviews.policy.digest='f'*64
        return result
    monkeypatch.setattr(module._Writer,'copy',copy)
    with pytest.raises(EvaluationError,match='changed_during_read'):f.freeze()
    assert not list(f.output.iterdir())


def test_corrupt_registered_source_cannot_fall_back_to_good_model_input(catalog_freeze):
    f=catalog_freeze
    with f.e.database.transaction() as connection:
        row=connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=? AND backend_id=?',
            (f.document.content_sha256,f.e.catalog.backend_id)).fetchone()
    backend=f.e.catalog.backends[f.e.catalog.backend_id]
    path=backend.root/row['object_key'];raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
    with pytest.raises(EvaluationError,match='registered_source_integrity'):f.freeze()
    assert not list(f.output.iterdir())


def test_registered_source_changed_after_copy_is_rechecked(catalog_freeze,monkeypatch):
    f=catalog_freeze
    import media_tag.topic_evaluation.exporter as module
    original=module._Writer.copy;changed=False
    with f.e.database.transaction() as connection:
        row=connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=? AND backend_id=?',
            (f.document.content_sha256,f.e.catalog.backend_id)).fetchone()
    path=f.e.catalog.backends[f.e.catalog.backend_id].root/row['object_key']
    def copy(self,*args):
        nonlocal changed
        result=original(self,*args)
        if not changed:
            changed=True;raw=bytearray(path.read_bytes());raw[-1]^=1;path.write_bytes(raw)
        return result
    monkeypatch.setattr(module._Writer,'copy',copy)
    with pytest.raises(EvaluationError,match='registered_source_integrity'):f.freeze()
    assert not list(f.output.iterdir())


@pytest.mark.parametrize('kind', ['audio','animation'])
def test_unconnected_modalities_export_explicit_failure_not_standalone_results(catalog_freeze,kind):
    from PIL import Image
    f=catalog_freeze;output=BytesIO()
    if kind=='audio':
        with wave.open(output,'wb') as wav:
            wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(8000);wav.writeframes(b'\0\0'*800)
        name='synthetic-unused-audio.wav'
    else:
        Image.new('RGB',(32,32),'red').save(output,format='GIF',save_all=True,
            append_images=[Image.new('RGB',(32,32),'blue')],duration=[100,100],loop=0)
        name='synthetic-unused-animation.gif'
    asset=f.p.register(output.getvalue(),name,kind)
    _,mapping=f.dataset_for(asset_id=asset.asset_id,source_sha=asset.sha256,kind=kind);f.mapping.update(mapping)
    result=f.freeze();exported=Export.model_validate_json(result['export_path'].read_bytes())
    expected='audio_topic_export_not_ready' if kind=='audio' else 'temporal_topic_export_not_ready'
    assert exported.predictions[0].failure_code==expected and exported.predictions[0].document is None
    report=evaluate(result['dataset_path'],result['export_path'])['report']
    assert report['latency']['measured_cases']==0
    assert report['overall']['structural_required_evaluations']==(8 if kind=='audio' else 10)


def test_unknown_temporal_stage_is_not_silently_ignored_for_older_success(catalog_freeze):
    f=catalog_freeze
    with f.e.database.transaction(write=True) as connection:
        row=connection.execute('SELECT spec_json FROM router_tasks WHERE task_id=?',(f.accepted['task_id'],)).fetchone()
        spec=json.loads(row[0]);spec['stage']='temporal_topics'
        connection.execute('UPDATE router_tasks SET spec_json=? WHERE task_id=?',(json.dumps(spec),f.accepted['task_id']))
    result=f.freeze();exported=Export.model_validate_json(result['export_path'].read_bytes())
    assert exported.predictions[0].failure_code=='topic_stage_export_not_supported'
    assert exported.predictions[0].document is None
