"""Isolated Router and mock protocol tests. No actual weights or GPU are used."""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import pytest

from search.conftest import sought, env
from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.contracts.models import TaskLimits, Versions
from media_tag.local_topics.contracts import Generation, LocalTopicDeployment, LocalTopicOptions
from media_tag.local_topics.projection import verified, merge
from media_tag.local_topics.prompt import PROMPT_VERSION
from media_tag.local_topics.runtime import LocalTopicHandlers, stage
from media_tag.local_topics.service import PREPROCESS_VERSION
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import JobSpec, StepSpec
from media_tag.router.runtime import LocalExecutor
from media_tag.semantic.registry import SemanticRegistry
from .test_topics import answer


@pytest.fixture
def topic_router(sought,tmp_path):
    e = sought.env
    with e.database.transaction(write=True) as connection:
        connection.execute('UPDATE router_jobs SET cancelled=1')
    registry = SemanticRegistry(tmp_path/'absent-weights')
    e.router.stages['local_topics'] = stage(registry)
    e.router.register_worker(e.worker.actor_id,('local_topics',),principal=e.admin)
    options = LocalTopicOptions(model_revision=registry.revision,policy_digest=sought.reviews.policy.digest)
    versions = Versions(rules_version='sha256:'+options.policy_digest,preprocess_version=PREPROCESS_VERSION,
        taxonomy_version=sought.reviews.policy.config.taxonomy_version,model_revision=registry.revision)
    spec = StepSpec(key='topics',stage='local_topics',config=options.model_dump(mode='json'),versions=versions,
        target_fields=TOPIC_FIELDS,prompt_version=PROMPT_VERSION,limits=TaskLimits(gpu_slots=1))
    submitted = e.router.submit(JobSpec(asset_id=sought.asset.asset_id,steps=(spec,)),request_id='local-topic-test',principal=e.owner)
    deployment = LocalTopicDeployment(enabled=True,model_root=str(registry.root))
    class MockHandler(LocalTopicHandlers):
        backend = 'local_qwen'
        def invoke(self,request,output,task,stopped,*,deadline):
            # Protocol mock advertises expected wire backend to exercise verifier.
            # This fixture never loads weights and never claims a real inference test.
            return Generation(backend=self.backend,model_revision=registry.revision,prompt_version=PROMPT_VERSION,
                image_sha256=request['image_sha256'],answers=tuple(answer(name,('red circle',))
                    if name=='英文检索词' else answer(name) for name in TOPIC_FIELDS))
    handler = MockHandler(e.router,e.worker,deployment)
    def rows():
        with e.database.transaction() as connection:
            row = connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at FROM router_tasks t
                LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                WHERE t.job_id=? AND t.active=1''',(submitted['job_id'],)).fetchone()
            asset = connection.execute('SELECT * FROM assets WHERE asset_id=?',(sought.asset.asset_id,)).fetchone()
            job = connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(submitted['job_id'],)).fetchone()
        return row,asset,job
    return SimpleNamespace(env=e,sought=sought,handler=handler,rows=rows,spec=spec,
        executor=LocalExecutor(e.router,e.worker,handler),job_id=submitted['job_id'])


def test_router_success_current_report_merge_and_human_preservation(topic_router):
    t = topic_router
    before = t.sought.reviews.detail(t.sought.asset.asset_id,principal=t.env.owner)
    assert t.executor.run_once() is not None
    row,asset,job = t.rows()
    assert row['state']=='succeeded', row['error_code']
    task,result,report,ref = verified(t.env.router,row,asset)
    assert len(report.result.document.fields)==10 and task.limits.gpu_slots==1
    assert report.native_input.sha256 == report.result.image.sha256
    with t.env.database.transaction() as connection:
        fields,evidence,producers,states = merge(t.env.router,connection,asset,'image',job,{},(),())
    assert fields['英文检索词'].value.values==('red circle',)
    assert len(evidence)==1 and len(producers)==1 and producers[0].stage=='local_topics'
    assert states['topics']=='verified_local_topic_draft'
    after = t.sought.reviews.detail(t.sought.asset.asset_id,principal=t.env.owner)
    before.pop('suggestions');after.pop('suggestions')
    assert after==before
    text = json.dumps(report.model_dump(mode='json'))
    assert str(t.env.workspace) not in text and t.sought.original.name not in text


def test_synthetic_backend_rejected_in_production_handler(topic_router):
    t=topic_router;t.handler.backend='synthetic_protocol'
    t.executor.run_once()
    row,asset,job=t.rows()
    assert row['state'] in ('failed','needs_attention') and row['error_code']=='local_topic_synthetic_result_rejected'


@pytest.mark.parametrize('change', ['active','released','cancelled','epoch'])
def test_current_attempt_requirements(topic_router,change):
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    with t.env.database.transaction(write=True) as connection:
        if change=='active':connection.execute('UPDATE router_tasks SET active=0 WHERE task_id=?',(row['task_id'],))
        elif change=='released':connection.execute('UPDATE router_attempts SET released_at=NULL WHERE task_id=?',(row['task_id'],))
        elif change=='cancelled':connection.execute('UPDATE router_jobs SET cancelled=1 WHERE job_id=?',(job['job_id'],))
        else:connection.execute('UPDATE router_tasks SET lease_epoch=lease_epoch+1 WHERE task_id=?',(row['task_id'],))
    with pytest.raises(ValueError,match='current_attempt'):
        verified(t.env.router,row,asset)


def test_unreleased_never_merges_previous_success(topic_router):
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    with t.env.database.transaction(write=True) as connection:
        connection.execute('UPDATE router_attempts SET released_at=NULL WHERE task_id=?',(row['task_id'],))
    with t.env.database.transaction() as connection:
        fields,evidence,producers,states=merge(t.env.router,connection,asset,'image',job,
            {f.field:f for f in verified_report_fields(t,row)},(),())
    assert all(f.value.state=='unknown' for f in fields.values()) and not evidence and not producers


def verified_report_fields(t,row):
    result=json.loads(row['result_json'])
    ref=next(r for r in result['artifacts'] if r['kind']=='local_topic_report')
    from media_tag.local_topics.contracts import RouterTopicReport
    with t.env.router.files.open_read(ref['ref']) as stream:
        return RouterTopicReport.model_validate_json(stream.read()).result.document.fields


def test_missing_artifact_cannot_fall_back(topic_router):
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    result=json.loads(row['result_json'])
    ref=next(r for r in result['artifacts'] if r['kind']=='local_topic_model_image')
    (t.env.workspace/ref['ref']).unlink()
    with t.env.database.transaction() as connection:
        fields,evidence,producers,states=merge(t.env.router,connection,asset,'image',job,{},(),())
    assert states['topics']=='unavailable' and not evidence and not producers
    assert all(f.value.state=='failed' and f.next_step=='evidence' for f in fields.values())


def test_policy_changed_rejects_success(topic_router):
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    t.sought.reviews.policy.digest='f'*64
    with pytest.raises(ValueError,match='binding'):
        verified(t.env.router,row,asset)


def test_normalized_source_is_recomputed(topic_router,monkeypatch):
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    import media_tag.local_topics.projection as projection
    monkeypatch.setattr(projection,'prepare_image',lambda raw,options:b'different-pixels')
    with pytest.raises(ValueError,match='not_normalized_from_source'):
        verified(t.env.router,row,asset)


def test_strict_options_and_required_gpu_lease(topic_router):
    t=topic_router
    with pytest.raises(ValueError):
        LocalTopicOptions.model_validate({**t.spec.config,'synthetic':True})
    with pytest.raises(ValueError):
        t.env.router.submit(JobSpec(asset_id=t.sought.asset.asset_id,steps=(t.spec.model_copy(update={
            'limits':TaskLimits(gpu_slots=0)}),)),request_id='wrong-gpu',principal=t.env.owner)


def rewrite_report(t,row,change):
    """Simulate consistent byte/hash tampering; actual semantic bindings must still fail."""
    from media_tag.contracts.models import TaskEnvelope
    from media_tag.local_topics.runtime import suggestions
    from media_tag.local_topics.contracts import RouterTopicReport
    payload=json.loads(row['result_json'])
    ref=next(r for r in payload['artifacts'] if r['kind']=='local_topic_report')
    path=t.env.workspace/ref['ref']
    report=json.loads(path.read_text(encoding='utf8'))
    change(report,payload)
    typed=RouterTopicReport.model_validate_json(json.dumps(report))
    payload['suggestions']=[s.model_dump(mode='json') for s in suggestions(typed.result.document,
        TaskEnvelope.model_validate_json(row['envelope_json']))]
    payload['evidence']=[e.model_dump(mode='json') for e in typed.result.document.evidence]
    raw=json.dumps(report,ensure_ascii=False).encode()
    path.write_bytes(raw);ref['sha256']=sha256(raw).hexdigest()
    with t.env.database.transaction(write=True) as connection:
        connection.execute('UPDATE router_attempts SET result_json=? WHERE task_id=? AND lease_epoch=?',
            (json.dumps(payload,ensure_ascii=False),row['task_id'],row['lease_epoch']))
    return t.rows()[0]


def test_verified_also_rejects_synthetic_report(topic_router):
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    row=rewrite_report(t,row,lambda report,result:report['result'].update(backend='synthetic_protocol'))
    with pytest.raises(ValueError,match='report_binding'):
        verified(t.env.router,row,asset)


def test_recomputed_answers_reject_consistent_field_rewrite(topic_router):
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    def change(report,payload):
        item=next(f for f in report['result']['document']['fields'] if f['field']=='英文检索词')
        item['value']['values']=['blue square']
    row=rewrite_report(t,row,change)
    with pytest.raises(ValueError,match='normalization_changed'):
        verified(t.env.router,row,asset)


def test_valid_large_canonical_png_is_accepted_within_runner_budget(topic_router,tmp_path):
    import random
    from PIL import Image
    from media_tag.storage.filesystem import FilesystemBackend
    t,e=topic_router,topic_router.env
    e.router.cancel(t.job_id,principal=e.owner)
    source=tmp_path/'canonical-large-source';source.mkdir()
    (source/'README.md').write_text('Synthetic high entropy image; protocol only, no model call.\n')
    Image.frombytes('RGB',(2048,2048),random.Random(19).randbytes(2048*2048*3)).save(source/'large.jpg',quality=90)
    asset=e.catalog.register_file(FilesystemBackend(source),'large.jpg',source_backend_id='synthetic-large',
        principal=e.owner,request_id='large-canonical',name='Large synthetic image')
    job=e.router.submit(JobSpec(asset_id=asset.asset_id,steps=(t.spec,)),request_id='large-canonical-job',principal=e.owner)
    assert t.executor.run_once()['accepted']
    with e.database.transaction() as connection:
        row=connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at FROM router_tasks t
            JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.job_id=?''',(job['job_id'],)).fetchone()
        current=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset.asset_id,)).fetchone()
    _,_,report,_=verified(e.router,row,current)
    assert 4*1024**2<(e.workspace/report.native_input.ref).stat().st_size<16*1024**2


def test_source_derivation_rejects_consistent_image_and_report_rewrite(topic_router):
    from PIL import Image
    from io import BytesIO
    t=topic_router;t.executor.run_once();row,asset,job=t.rows()
    blue=BytesIO();Image.new('RGB',(24,24),'blue').save(blue,format='PNG')
    canonical=blue.getvalue();digest=sha256(canonical).hexdigest()
    def change(report,payload):
        local=report['result']
        for ref in (local['image'],report['native_input']):
            (t.env.workspace/ref['ref']).write_bytes(canonical);ref['sha256']=digest
        local['document']['evidence'][0]['artifact']['sha256']=digest
        raw_path=t.env.workspace/local['raw_report']['ref']
        raw=json.loads(raw_path.read_text(encoding='utf8'))
        raw['image']['sha256']=digest;raw['generation']['image_sha256']=digest
        raw_bytes=json.dumps(raw,ensure_ascii=False).encode();raw_path.write_bytes(raw_bytes)
        local['raw_report']['sha256']=sha256(raw_bytes).hexdigest()
        replacement={r['ref']:r['sha256'] for r in (local['image'],report['native_input'],local['raw_report'])}
        for ref in payload['artifacts']:
            if ref['ref'] in replacement:ref['sha256']=replacement[ref['ref']]
    row=rewrite_report(t,row,change)
    with pytest.raises(ValueError,match='not_normalized_from_source'):
        verified(t.env.router,row,asset)
