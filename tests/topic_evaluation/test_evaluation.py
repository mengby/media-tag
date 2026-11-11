from copy import deepcopy
from hashlib import sha256
import json
import subprocess
import sys

import pytest
from media_tag.topic_evaluation import Dataset,Export,evaluate,propose_rules
from media_tag.topic_evaluation.io import Bundle,EvaluationError,load,strict_json
from media_tag.contracts.models import ArtifactRef


def test_ten_visual_eight_audio_denominators_and_immutable_report(frozen):
    frozen.add();frozen.add('audio','audio',elapsed=9.)
    dataset,export=frozen.save();result=evaluate(dataset,export,output_directory=frozen.output)
    report=result['report']
    assert report['overall']['applicable_fields']==18 and report['overall']['not_applicable_fields']==2
    assert report['overall']['exact_match_labelled']=={'numerator':18,'denominator':18,'value':1.}
    assert report['per_modality']['audio']['applicable_fields']==8
    assert report['per_modality']['image']['applicable_fields']==10
    assert report['latency']['p95_seconds']==9. and report['latency']['measured_cases']==2
    assert report['purpose']=='synthetic_engineering' and not report['business_acceptance'] and not report['calibrated']
    raw=result['path'].read_bytes();assert sha256(raw).hexdigest()==result['sha256']
    from media_tag.topic_evaluation.io import publish
    path,digest=publish(frozen.output,report);assert path==result['path'] and path.read_bytes()==raw


@pytest.mark.parametrize('state', ['unknown','no_match','failed'])
def test_abstention_and_failure_never_count_as_success(frozen,state):
    frozen.add(states={field:state for field in ('画面主体','中文检索词','英文检索词')})
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['overall']['exact_match_labelled']['numerator']==7
    assert report['overall']['exact_match_labelled']['denominator']==10
    assert report['per_field']['画面主体']['label_recall']['value']==0.
    assert report['per_field']['画面主体']['state_counts']=={state:1}


def test_medical_false_positive_and_direction_error_independent(frozen):
    case,prediction=frozen.add(kind='animation',values={'科室':['wrong-department'],'过程/状态':['decrease']})
    state=next(g for g in case['gold'] if g['field']=='过程/状态')
    state.update(values=['increase'],opposite_direction_values=['decrease'])
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['overall']['medical_false_positive_cases']==2
    assert report['overall']['direction_error_cases']==1
    assert report['per_field']['科室']['direction_error_cases']==0
    assert report['per_field']['过程/状态']['label_precision']['value']==0.


def test_missing_evidence_keeps_all_denominators(frozen):
    case,prediction=frozen.add();(frozen.root/'visual-source.bin').unlink()
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['overall']['applicable_fields']==10
    assert report['overall']['evidence_missing']==10 and report['overall']['exact_match_labelled']['numerator']==0
    assert report['overall']['label_precision']['value']==0.


def test_missing_producer_is_not_verified_success(frozen):
    case,prediction=frozen.add()
    frozen.rewrite_document(prediction,lambda doc:[item.update(producer_ids=[]) for item in doc['fields']])
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['overall']['producer_missing']==10 and report['overall']['exact_match_labelled']['numerator']==0


def test_missing_predictions_and_explicit_timeouts_remain_in_denominator(frozen):
    frozen.add();case,prediction=frozen.add('timeout',elapsed=12.)
    prediction.pop('document');prediction['failure_code']='model_timeout'
    case,prediction=frozen.add('missing');frozen.predictions.pop()
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['overall']['applicable_fields']==30 and report['overall']['failures']==20
    assert report['overall']['exact_match_labelled']['numerator']==10
    assert report['latency']['measured_cases']==2 and report['latency']['p95_seconds']==12.
    assert report['overall']['reason_counts']['model_timeout']==10


@pytest.mark.parametrize('leak', ['sha','group'])
def test_cross_split_source_and_group_leakage(frozen,leak):
    first,_=frozen.add('cal',split='calibration');second,_=frozen.add('accept')
    second['source_sha256' if leak=='sha' else 'near_group']=first['source_sha256' if leak=='sha' else 'near_group']
    with pytest.raises(ValueError,match='split_leakage'):frozen.save()


def test_training_leakage_also_rejected(frozen):
    first,_=frozen.add('train',split='training');second,_=frozen.add('accept')
    second['near_group']=first['near_group']
    with pytest.raises(ValueError,match='near_group_split_leakage'):frozen.save()


@pytest.mark.parametrize('change', ['model','prompt','source'])
def test_frozen_producer_and_source_pins_reject_drift(frozen,change):
    case,prediction=frozen.add()
    def mutate(doc):
        if change=='source':
            doc['content_sha256']='b'*64
            for evidence in doc['evidence']:evidence['source_sha256']='b'*64
            for producer in doc['producers']:producer['source_sha256']='b'*64
        elif change=='model':
            for field in doc['fields']:field['versions']['model_revision']='changed-model'
            for producer in doc['producers']:producer['versions']['model_revision']='changed-model'
        else:doc['producers'][0]['prompt_version']='changed-prompt'
    frozen.rewrite_document(prediction,mutate);dataset,export=frozen.save()
    with pytest.raises(EvaluationError):evaluate(dataset,export)


def test_model_self_confidence_is_not_an_imported_accuracy(frozen):
    frozen.add();dataset,export=frozen.save();payload=json.loads(export.read_text(encoding='utf8'))
    payload['predictions'][0]['confidence']=1.
    export.write_text(json.dumps(payload),encoding='utf8')
    with pytest.raises(ValueError):evaluate(dataset,export)


def test_synthetic_annotators_cannot_be_relabelled_business(frozen):
    frozen.add();dataset,export=frozen.save();payload=json.loads(dataset.read_text(encoding='utf8'))
    payload.update(purpose='business',independently_human_annotated=True)
    with pytest.raises(ValueError,match='actual_annotators'):Dataset.model_validate_json(json.dumps(payload))


def test_calibration_proposals_never_activate_synthetic_or_medical(frozen):
    frozen.add('calibration',split='calibration');frozen.add('acceptance')
    dataset,export=frozen.save()
    calibration=evaluate(dataset,export,split='calibration')['report']
    proposed=propose_rules(calibration,minimum_groups=1)
    assert not proposed['calibrated'] and not proposed['automatic_enablement']
    assert all('synthetic_only' in item['reasons'] and not item['automatic_enablement'] for item in proposed['candidates'])
    assert all('medical_fields_always_require_human_review' in item['reasons'] for item in proposed['candidates'] if item['field']=='科室')
    acceptance=evaluate(dataset,export)['report']
    with pytest.raises(EvaluationError,match='must_not_tune'):propose_rules(acceptance)


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":Infinity}'])
def test_duplicate_keys_and_nonfinite_json_rejected(raw):
    with pytest.raises(ValueError):strict_json(raw)


def test_bundle_limits_cached_failure_and_path_escape(frozen):
    data=b'x'*30;(frozen.root/'large.bin').write_bytes(data)
    ref=ArtifactRef(kind='test',ref='large.bin',sha256=sha256(data).hexdigest())
    bundle=Bundle(frozen.root,max_file_bytes=10,max_total_bytes=20)
    with pytest.raises(EvaluationError,match='budget'):bundle.read(ref)
    count=bundle.bytes_read
    with pytest.raises(EvaluationError,match='cached'):bundle.read(ref)
    assert count==11 and bundle.bytes_read==count
    with pytest.raises(ValueError):ArtifactRef(kind='test',ref='../outside',sha256='a'*64)


def test_cli_schema_and_actual_offline_report(frozen):
    frozen.add();dataset,export=frozen.save()
    schema=subprocess.run([sys.executable,'-m','media_tag.topic_evaluation','--schema','dataset'],capture_output=True,text=True,encoding='utf8')
    assert schema.returncode==0 and 'ct-topic-evaluation-dataset-v1' in schema.stdout
    result=subprocess.run([sys.executable,'-m','media_tag.topic_evaluation','--dataset',str(dataset),'--export',str(export),
        '--output',str(frozen.output)],capture_output=True,text=True,encoding='utf8')
    assert result.returncode==0,result.stderr
    output=json.loads(result.stdout)
    assert not output['calibrated'] and not output['business_acceptance']


def test_negative_abstention_is_diagnostic_not_exact_success(frozen):
    case,prediction=frozen.add(states={'疾病/症状':'unknown'})
    gold=next(g for g in case['gold'] if g['field']=='疾病/症状');gold['values']=[]
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['per_field']['疾病/症状']['appropriate_negative_abstentions']==1
    assert report['per_field']['疾病/症状']['exact_match_labelled']['numerator']==0
    assert report['overall']['medical_false_positive_cases']==0


def test_unassessable_annotation_not_silently_deleted_from_coverage(frozen):
    case,prediction=frozen.add()
    gold=next(g for g in case['gold'] if g['field']=='画面主体')
    gold.update(state='unassessable',complete_value_set=False,values=[])
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['overall']['applicable_fields']==10 and report['overall']['labelled_fields']==9
    assert report['overall']['unassessable_fields']==1
    assert report['overall']['exact_match_labelled']['value']==1.
    assert report['overall']['end_to_end_exact_coverage']['value']==.9
    assert report['overall']['applicability_accuracy_required']['value']==.9
    assert report['overall']['applicability_unresolved']==1


def test_document_corruption_counts_failure_not_old_success(frozen):
    case,prediction=frozen.add();dataset,export=frozen.save()
    (frozen.root/prediction['document']['ref']).write_bytes(b'changed bytes')
    report=evaluate(dataset,export)['report']
    assert report['overall']['failures']==10 and report['overall']['evidence_missing']==10
    assert report['overall']['exact_match_labelled']['numerator']==0


def test_explicit_export_taxonomy_hash_must_match_dataset(frozen):
    frozen.add();dataset,export=frozen.save()
    payload=json.loads(export.read_text(encoding='utf8'))
    payload.update(taxonomy_version='synthetic-other-version',taxonomy_sha256='a'*64)
    export.write_text(json.dumps(payload),encoding='utf8')
    with pytest.raises(EvaluationError,match='taxonomy_binding'):evaluate(dataset,export)


def test_observed_group_support_cannot_use_unresolved_groups(frozen):
    for index in range(6):
        frozen.add('cal-'+str(index),split='calibration',states={'画面主体':'unknown'} if index else {})
    dataset,export=frozen.save();report=evaluate(dataset,export,split='calibration')['report']
    proposal=propose_rules(report,minimum_groups=5)
    subject=next(c for c in proposal['candidates'] if c['field']=='画面主体')
    assert subject['independent_observed_groups']==1
    assert 'insufficient_independent_observed_groups' in subject['reasons']


def test_cli_refuses_acceptance_rule_tuning_before_output(frozen):
    frozen.add();dataset,export=frozen.save()
    result=subprocess.run([sys.executable,'-m','media_tag.topic_evaluation','--dataset',str(dataset),'--export',str(export),
        '--output',str(frozen.output),'--propose-rules'],capture_output=True,text=True,encoding='utf8')
    assert result.returncode==2 and 'requires --split calibration' in result.stderr
    assert not list(frozen.output.iterdir())


def test_reverse_unmatched_candidate_blocks_rule_even_when_effective_label_matches(frozen):
    case,prediction=frozen.add(kind='animation',split='calibration')
    gold=next(g for g in case['gold'] if g['field']=='英文检索词')
    gold['opposite_direction_values']=['reverse motion']
    def change(doc):
        field=next(f for f in doc['fields'] if f['field']=='英文检索词')
        field.update(candidates=['reverse motion'],dynamic=True)
    frozen.rewrite_document(prediction,change)
    dataset,export=frozen.save();report=evaluate(dataset,export,split='calibration')['report']
    assert report['per_field']['英文检索词']['exact_match_labelled']['value']==1.
    assert report['per_field']['英文检索词']['candidate_direction_error_cases']==1
    candidate=next(c for c in propose_rules(report,minimum_groups=1)['candidates'] if c['field']=='英文检索词')
    assert 'integrity_failure_or_direction_error' in candidate['reasons']
    assert candidate['keep_non_observed_in_precision'] and not candidate['automatic_enablement']


def test_rule_support_excludes_cases_that_fail_its_no_candidate_precondition(frozen):
    for index in range(6):
        case,prediction=frozen.add('cal-'+str(index),split='calibration')
        def change(doc):
            subject=next(f for f in doc['fields'] if f['field']=='画面主体')
            subject['candidates']=['potential ambiguity']
        frozen.rewrite_document(prediction,change)
    dataset,export=frozen.save();report=evaluate(dataset,export,split='calibration')['report']
    subject=next(c for c in propose_rules(report)['candidates'] if c['field']=='画面主体')
    assert subject['independent_observed_groups']==0 and subject['observed_denominator']==0
    assert 'insufficient_independent_observed_groups' in subject['reasons']
