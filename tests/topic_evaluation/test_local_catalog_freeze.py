"""Actual local-topic Router wiring with the explicitly mocked Qwen protocol."""
from datetime import datetime,timedelta,timezone
import json

from local_topics.test_router import topic_router,sought,env
from upload.conftest import tools
from media_tag.analysis.intake import ensure_intent
from media_tag.analysis.service import AnalysisService
from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.projection import TopicProjection
from media_tag.router.runtime import CPUHandlers,LocalExecutor,cpu_stages
from media_tag.topic_evaluation import Dataset,Export,freeze_catalog_export,evaluate


def test_current_local_topics_freeze_preserves_unknown_fields_and_actual_protocol_artifacts(topic_router,tools,tmp_path):
    t=topic_router;e=t.env
    e.router.cancel(t.job_id,principal=e.owner)
    e.router.stages.update(cpu_stages(tools))
    e.router.register_worker(e.worker.actor_id,('probe','evidence','local_topics'),principal=e.admin)
    analysis=AnalysisService(e.router,AnalysisDeployment(enabled=True))
    with e.database.transaction(write=True) as connection:ensure_intent(connection,t.sought.asset.asset_id,e.owner.actor_id,content_kind='image')
    analysis.dispatch_pending(principal=e.owner)
    cpu=CPUHandlers(e.router,e.worker,tools)
    executor=LocalExecutor(e.router,e.worker,lambda lease,stopped:t.handler(lease,stopped) if lease.task.stage=='local_topics' else cpu(lease,stopped))
    for _ in range(12):
        if executor.run_once() is None:break
    state=analysis.status(t.sought.asset.asset_id,principal=e.owner)
    with e.database.transaction() as connection:
        asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(t.sought.asset.asset_id,)).fetchone()
        job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(state['runtime']['job_id'],)).fetchone()
        document=TopicProjection(e.router).build(connection,asset,'image',job)[0]
    gold=[{'field':f.field,'state':'labelled' if f.value.state=='observed' else 'unassessable',
        'complete_value_set':f.value.state=='observed','values':list(f.value.values or ()),
        'annotation_note':'Synthetic mock protocol expectation only; no real Qwen inference or annotation accuracy.'} for f in document.fields]
    data=Dataset.model_validate_json(json.dumps({'dataset_id':'synthetic-local-topic-freeze','purpose':'synthetic_engineering',
        'frozen_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),'annotators':['synthetic-annotator'],
        'adjudicator':'synthetic-adjudicator','independently_human_annotated':False,'annotation_reference':'Synthetic wiring only.',
        'taxonomy_version':t.sought.reviews.policy.config.taxonomy_version,'taxonomy_sha256':t.sought.reviews.policy.config.taxonomy_manifest_sha256,
        'runs':[{'run_id':'synthetic-qwen-protocol','producer_pins':[{'versions':p.versions.model_dump(mode='json'),
            'prompt_version':p.prompt_version} for p in document.producers]}],
        'cases':[{'case_id':'synthetic-case','source_sha256':asset['blob_sha256'],'content_kind':'image','near_group':'synthetic-source',
            'split':'acceptance','annotation_reference':'Synthetic actual image bytes only.','gold':gold}]}))
    inputs=tmp_path/'local-freeze-inputs';inputs.mkdir();(inputs/'README.md').write_text('Synthetic local-topic freeze inputs.')
    dataset=inputs/'dataset.json';dataset.write_text(data.model_dump_json(),encoding='utf8')
    output=tmp_path/'local-private-freeze';output.mkdir()
    result=freeze_catalog_export(e.router,dataset,{'synthetic-case':asset['asset_id']},'synthetic-qwen-protocol',
        principal=e.owner,private_output=output)
    exported=Export.model_validate_json(result['export_path'].read_bytes())
    copied=json.loads((result['directory']/exported.predictions[0].document.ref).read_text(encoding='utf8'))
    assert any(p['stage']=='local_topics' for p in copied['producers'])
    assert next(f for f in copied['fields'] if f['field']=='过程/状态')['value']['state']=='unknown'
    report=evaluate(result['dataset_path'],result['export_path'])['report']
    assert report['overall']['structural_required_evaluations']==10 and report['overall']['exact_match_labelled']['numerator']==1
    assert not report['calibrated'] and not report['business_acceptance']
