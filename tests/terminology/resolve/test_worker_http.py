from hashlib import sha256
import json
import sqlite3
import pytest
from fastapi.testclient import TestClient
from media_tag.catalog import Principal,Database
from media_tag.contracts.models import TaskLimits,Versions
from media_tag.router.models import JobSpec,StepSpec
from media_tag.router.runtime import LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.terminology.resolve.models import ResolveInput,ResolveTask,PREPROCESS_VERSION,ResolveOptions
from media_tag.terminology.resolve.service import Resolver
from media_tag.workers.terminology import terminology_stages,TerminologyHandlers


def prepare_job(resolved,tmp_path,*,origin='human_input',query='合成圆圈'):
    env=resolved.env;originals=tmp_path/'originals';originals.mkdir();path=originals/'synthetic.input';path.write_bytes(b'Synthetic D14 immutable source')
    asset=env.catalog.register_file(FilesystemBackend(originals),'synthetic.input',source_backend_id='synthetic',principal=env.owner,request_id='source',name='合成圆圈')
    env.router.stages.update(terminology_stages(resolved.resolver.registry))
    env.router.register_worker(env.worker.actor_id,('terminology_resolve',),principal=env.admin)
    options=ResolveTask(taxonomy_release_id=resolved.tax['release_id'],lexicon_release_id=resolved.lex['release_id'],
        embedding_index_id=resolved.index['index_id'],request=ResolveInput(query=query,purpose='tag',evidence_origin=origin))
    step=StepSpec(key='terms',stage='terminology_resolve',config=options.model_dump(mode='json'),versions=Versions(rules_version=env.contract.version,
        preprocess_version=PREPROCESS_VERSION,taxonomy_version=resolved.tax['version'],lexicon_version=resolved.lex['version'],model_revision=resolved.resolver.registry.revision),
        limits=TaskLimits(timeout_seconds=30,max_attempts=1,gpu_slots=0))
    job=env.router.submit(JobSpec(asset_id=asset.asset_id,steps=(step,)),request_id='tag-task',principal=env.owner)
    return asset,path,step,job


@pytest.mark.parametrize('origin',['human_input','old_filename'])
def test_real_router_envelope_adopts_candidate_report_without_human_values(resolved,tmp_path,origin):
    env=resolved.env;asset,path,_,job=prepare_job(resolved,tmp_path,origin=origin)
    before=sha256(path.read_bytes()).hexdigest()
    executor=LocalExecutor(env.router,env.worker,TerminologyHandlers(resolved.resolver))
    assert executor.run_once()['accepted']
    current=env.router.get_job(job['job_id'],principal=env.owner)
    assert current['state']=='succeeded' and not current['resources']
    with env.database.transaction() as c:
        result=json.loads(c.execute('SELECT result_json FROM router_attempts WHERE task_id=?',(current['tasks'][0]['task_id'],)).fetchone()[0])
        assert result['suggestions']==[]
        ref=result['artifacts'][0]
        report=json.loads((env.workspace/ref['ref']).read_bytes())
        assert report['source_sha256']==asset.sha256 and report['state']=='resolved'
        assert not report['medical_evidence_eligible'] and not report['effective_values_written']
        assert c.execute('SELECT count(*) FROM effective_values').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM index_confirmations').fetchone()[0]==0
    assert sha256(path.read_bytes()).hexdigest()==before
    assert env.catalog.get_asset(asset.asset_id,principal=env.owner).revision==asset.revision


@pytest.mark.parametrize('origin',['ocr','visual_description','prior_quote'])
def test_unsubstantiated_source_claims_fail_instead_of_becoming_evidence(resolved,tmp_path,origin):
    env=resolved.env;asset,path,_,job=prepare_job(resolved,tmp_path,origin=origin)
    executor=LocalExecutor(env.router,env.worker,TerminologyHandlers(resolved.resolver))
    assert executor.run_once()['accepted']
    result=env.router.get_job(job['job_id'],principal=env.owner)
    assert result['state']=='needs_attention' and not result['resources']
    assert result['tasks'][0]['error_code'] in ('text_origin_unavailable','text_origin_mismatch')


def test_task_rejects_gpu_medical_targets_and_wrong_model_revision(resolved,tmp_path):
    env=resolved.env;asset,_,step,_=prepare_job(resolved,tmp_path)
    for changed in [step.model_copy(update={'target_fields':('疾病/症状',)}),
                    step.model_copy(update={'limits':TaskLimits(timeout_seconds=30,max_attempts=1,gpu_slots=1)}),
                    step.model_copy(update={'versions':step.versions.model_copy(update={'model_revision':'wrong'})})]:
        with pytest.raises(ValueError):env.router.submit(JobSpec(asset_id=asset.asset_id,steps=(changed,)),request_id='bad',principal=env.owner)


def test_ocr_must_match_an_actual_released_parent_report(resolved,tmp_path):
    from media_tag.contracts.models import ContractModel,ArtifactRef
    from media_tag.router.models import StageDefinition
    env=resolved.env;asset,_,step,old=prepare_job(resolved,tmp_path,origin='ocr')
    env.router.cancel(old['job_id'],principal=env.owner)
    class Empty(ContractModel):pass
    env.router.stages['ocr']=StageDefinition(Empty,'synthetic-ocr-protocol-only',synthetic=True)
    env.router.register_worker(env.worker.actor_id,('ocr','terminology_resolve'),principal=env.admin)
    parent=StepSpec(key='ocr',stage='ocr',versions=Versions(rules_version=env.contract.version,preprocess_version='synthetic-ocr-protocol-only'))
    child=step.model_copy(update={'depends_on':('ocr',)})
    job=env.router.submit(JobSpec(asset_id=asset.asset_id,steps=(parent,child)),request_id='ocr-terms',principal=env.owner)
    handler=TerminologyHandlers(resolved.resolver)
    def protocol(lease,stopped):
        if lease.task.stage!='ocr':return handler(lease,stopped)
        folder=env.workspace/lease.task.output_prefix;folder.mkdir(parents=True)
        (folder/'README.md').write_text('Synthetic OCR PROTOCOL fixture; no model inference claimed.')
        path=folder/'ocr.json';path.write_text(json.dumps({'kind':'ocr','source_sha256':asset.sha256,'synthetic_protocol':True,
            'frames':[{'observation':{'lines':[{'text':'合成圆圈'}]}}]}),encoding='utf-8')
        return {'status':'succeeded','artifacts':(ArtifactRef(kind='model_report',ref=path.relative_to(env.workspace).as_posix(),sha256=sha256(path.read_bytes()).hexdigest()),)}
    executor=LocalExecutor(env.router,env.worker,protocol)
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    done=env.router.get_job(job['job_id'],principal=env.owner)
    assert done['state']=='succeeded'
    with env.database.transaction() as c:
        row=c.execute("SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=? AND step_key='terms'",(job['job_id'],)).fetchone()
    artifact=json.loads(row[0])['artifacts'][0]
    report=json.loads((env.workspace/artifact['ref']).read_bytes())
    assert report['evidence_ref']['kind']=='model_report' and report['evidence_origin']=='ocr'
    assert not report['medical_evidence_eligible']


def test_http_roles_source_claims_csrf_and_model_disabled_fallback(resolved):
    with TestClient(resolved.app,headers={'Authorization':'Bearer owner'}) as client:
        assert client.post('/terminology/resolve',json={'query':'合成圆圈'}).json()['state']=='resolved'
        assert client.post('/terminology/resolve',json={'query':'合成圆圈','purpose':'tag'}).status_code==422
        assert client.post('/terminology/resolve',json={'query':'合成圆圈','evidence_origin':'ocr'}).status_code==422
        assert client.post('/terminology/text-indices/activate',json=resolved.activation.model_dump(mode='json')).status_code==403
        assert client.get('/terminology/resolve/status').json()['state']=='ready'
    assert TestClient(resolved.app).post('/terminology/resolve',json={'query':'合成圆圈'}).status_code==401
    reader=TestClient(resolved.app,headers={'Authorization':'Bearer reader'})
    assert reader.post('/terminology/resolve',json={'query':'合成圆圈'}).status_code==200
    worker=TestClient(resolved.app,headers={'Authorization':'Bearer worker'})
    assert worker.post('/terminology/resolve',json={'query':'合成圆圈'}).status_code==403
    browser=TestClient(resolved.app)
    assert browser.post('/ui/session',headers={'Authorization':'Bearer owner','Origin':'http://testserver'}).status_code==200
    assert browser.post('/terminology/resolve',json={'query':'合成圆圈'},headers={'Origin':'https://untrusted.invalid'}).status_code==403
    no_model=Resolver(resolved.env.service,ResolveOptions(enabled=True))
    assert no_model.resolve(ResolveInput(query='合成圆圈'),principal=resolved.env.owner)['state']=='resolved'
    assert no_model.resolve(ResolveInput(query='新的语句'),principal=resolved.env.owner)['state']=='degraded'


def test_migration7_failure_rolls_back_and_preserves_old_release_data(tmp_path,monkeypatch):
    import media_tag.catalog.database as module
    path=tmp_path/'v7.sqlite3';c=sqlite3.connect(path)
    for version,statements in [(1,module.SCHEMA),(2,module.ROUTER_SCHEMA),(3,module.UPLOAD_SCHEMA),(4,module.REDACTION_SCHEMA),
                               (5,module.LIBRARY_SCHEMA),(6,module.REVIEW_SCHEMA),(7,module.TERMINOLOGY_SCHEMA)]:
        for statement in statements:c.execute(statement)
        c.execute('INSERT INTO schema_migrations VALUES(?,?,?)',(version,sha256('\n'.join(statements).encode()).hexdigest(),'synthetic'))
    c.execute("INSERT INTO catalog_metadata VALUES('synthetic','retained')");c.execute('PRAGMA user_version=7');c.commit();c.close()
    valid=module.TEXT_INDEX_SCHEMA;monkeypatch.setattr(module,'TEXT_INDEX_SCHEMA',(*valid,'invalid SQL'))
    with pytest.raises(sqlite3.OperationalError):Database(path).initialize()
    with Database(path).transaction() as c:assert c.execute('PRAGMA user_version').fetchone()[0]==7
    monkeypatch.setattr(module,'TEXT_INDEX_SCHEMA',valid);Database(path).initialize()
    with Database(path).transaction() as c:
        assert c.execute('PRAGMA user_version').fetchone()[0]==9
        assert c.execute('SELECT value FROM catalog_metadata').fetchone()[0]=='retained'
