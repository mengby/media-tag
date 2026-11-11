"""Actual synthetic WAV/bridge receipts through the connected local-audio projection."""
from hashlib import sha256
import json

import pytest
from .test_catalog_freeze import catalog_freeze,precise,sought,env,tools
from audio_topics.test_projection import run_local
from media_tag.analysis.projection import TopicProjection
from media_tag.catalog.service import encode
from media_tag.topic_evaluation import Export,evaluate


def prepare_audio(f):
    asset,_,accepted=run_local(f.p)
    state=f.p.analysis.status(asset.asset_id,principal=f.e.owner)
    with f.e.database.transaction() as connection:
        row=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset.asset_id,)).fetchone()
        job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(state['runtime']['job_id'],)).fetchone()
        document=TopicProjection(f.e.router).build(connection,row,'audio',job)[0]
    assert any(p.stage=='audio_topics' for p in document.producers)
    _,mapping=f.dataset_for(asset_id=asset.asset_id,source_sha=asset.sha256,kind='audio',topic_document=document)
    f.mapping.update(mapping)
    return asset,accepted,document


def test_actual_wav_local_audio_projection_freezes_eight_required_fields_without_forged_visual_producers(catalog_freeze):
    f=catalog_freeze;asset,accepted,source=prepare_audio(f)
    result=f.freeze();exported=Export.model_validate_json(result['export_path'].read_bytes())
    document=json.loads((result['directory']/exported.predictions[0].document.ref).read_text(encoding='utf8'))
    assert {p['stage'] for p in document['producers']}=={'audio_topics'}
    for item in document['fields']:
        if item['field'] in ('画面主体','表现形式'):
            assert item['value']['state']=='not_applicable' and not item['producer_ids']
    report=evaluate(result['dataset_path'],result['export_path'])['report']
    assert report['overall']['structural_required_evaluations']==8
    assert report['overall']['structural_assessment_success']['numerator']==3
    assert report['overall']['state_counts']['unknown']==5
    assert report['overall']['correct_not_applicable']==0  # Structural visual NA has no forged media producer.
    assert report['overall']['total_field_assessments']==10
    assert not report['calibrated'] and not report['business_acceptance']
    source_report=json.loads((f.e.workspace/source.producers[0].report.ref).read_text(encoding='utf8'))
    assert source_report['analysis_layer']=='local_audio' and source_report['reply']


def test_consistent_audio_reply_rewrite_without_document_reassessment_is_not_exported(catalog_freeze):
    f=catalog_freeze;asset,accepted,source=prepare_audio(f)
    report_path=f.e.workspace/source.producers[0].report.ref
    report=json.loads(report_path.read_text(encoding='utf8'))
    field=next(item for item in report['reply']['fields'] if item['field']=='中文检索词')
    field['value']={'state':'observed','values':['changed synthetic reply only']}
    raw=json.dumps(report,ensure_ascii=False).encode();report_path.write_bytes(raw);digest=sha256(raw).hexdigest()
    with f.e.database.transaction(write=True) as connection:
        row=connection.execute('SELECT result_json FROM router_attempts WHERE task_id=?',(accepted['task_id'],)).fetchone()
        payload=json.loads(row[0])
        for artifact in payload['artifacts']:
            if artifact['kind']=='precision_report':artifact['sha256']=digest
        updated=encode(payload)
        connection.execute('UPDATE router_attempts SET result_json=?,result_digest=? WHERE task_id=?',
            (updated,sha256(updated.encode()).hexdigest(),accepted['task_id']))
    result=f.freeze();exported=Export.model_validate_json(result['export_path'].read_bytes())
    assert exported.predictions[0].document is None
    assert exported.predictions[0].failure_code=='current_topic_evidence_unavailable'
