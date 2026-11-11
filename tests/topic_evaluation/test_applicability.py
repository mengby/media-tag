import json

import pytest
from media_tag.topic_evaluation import evaluate,propose_rules


def gold_na(case,fields):
    for gold in case['gold']:
        if gold['field'] in fields:
            gold.update(state='not_applicable',complete_value_set=False,values=[],
                annotation_note='Explicit synthetic independent field applicability: no supported content for this field.')


def prediction_state(frozen,prediction,field,state,*,candidates=(),missing=None):
    def change(document):
        item=next(f for f in document['fields'] if f['field']==field)
        item.update(value={'state':state,'values':None,'reason':'synthetic-field-applicability-assessment'},
                    next_step='none' if state=='not_applicable' else 'precision',candidates=list(candidates))
        if missing=='evidence':item['evidence_ids']=[]
        if missing=='producer':item['producer_ids']=[]
        if missing=='actual_media_kind':
            record=dict(document['evidence'][0],evidence_id='synthetic-filename-only',kind='filename')
            document['evidence'].append(record)
            document['producers'][0]['evidence_ids'].append(record['evidence_id'])
            item['evidence_ids']=[record['evidence_id']]
    frozen.rewrite_document(prediction,change)


def test_life_visual_na_keeps_ten_evaluations_and_separate_label_denominator(frozen):
    case,prediction=frozen.add('life-visual')
    fields=('科室','器官/系统','疾病/症状','情绪/语气')
    gold_na(case,fields)
    for field in fields:prediction_state(frozen,prediction,field,'not_applicable')
    dataset,export=frozen.save();report=evaluate(dataset,export)['report'];metrics=report['overall']
    assert metrics['total_field_assessments']==10 and metrics['structural_required_evaluations']==10
    assert metrics['applicable_fields']==6 and metrics['labelled_fields']==6
    assert metrics['correct_not_applicable']==4 and metrics['additional_not_applicable_fields']==4
    assert metrics['exact_match_labelled']['denominator']==6
    assert metrics['structural_assessment_success']=={'numerator':10,'denominator':10,'value':1.}
    assert metrics['applicability_accuracy_required']['value']==1. and metrics['applicability_errors']==0
    assert all(row['annotation_note'] for row in report['fields'] if row['gold_state']=='not_applicable')


def test_bgm_per_field_na_keeps_eight_required_evaluations(frozen):
    case,prediction=frozen.add('ordinary-bgm','audio')
    fields=('科室','器官/系统','疾病/症状','过程/状态')
    gold_na(case,fields)
    for field in fields:prediction_state(frozen,prediction,field,'not_applicable')
    dataset,export=frozen.save();metrics=evaluate(dataset,export)['report']['overall']
    assert metrics['total_field_assessments']==10 and metrics['structural_required_evaluations']==8
    assert metrics['applicable_fields']==4 and metrics['not_applicable_fields']==6
    assert metrics['correct_not_applicable']==6 and metrics['additional_not_applicable_fields']==4
    assert metrics['structural_assessment_success']=={'numerator':8,'denominator':8,'value':1.}


@pytest.mark.parametrize('field', ['画面主体','表现形式'])
def test_audio_visual_gold_still_mandatorily_na(frozen,field):
    case,_=frozen.add('bgm','audio')
    gold=next(g for g in case['gold'] if g['field']==field)
    gold.update(state='labelled',complete_value_set=True,values=['invented visual label'])
    with pytest.raises(ValueError,match='pure_audio_visual_fields_require_not_applicable'):frozen.save()


def test_model_na_against_labelled_gold_is_incorrect_not_success(frozen):
    case,prediction=frozen.add()
    prediction_state(frozen,prediction,'情绪/语气','not_applicable')
    dataset,export=frozen.save();metrics=evaluate(dataset,export)['report']['overall']
    assert metrics['applicable_fields']==10 and metrics['correct_not_applicable']==0
    assert metrics['incorrect_not_applicable']==1 and metrics['applicability_errors']==1
    assert metrics['exact_match_labelled']['numerator']==9
    assert metrics['structural_assessment_success']['numerator']==9


@pytest.mark.parametrize('state', ['unknown','no_match','failed'])
def test_unresolved_is_not_correct_na(frozen,state):
    case,prediction=frozen.add()
    gold_na(case,('情绪/语气',));prediction_state(frozen,prediction,'情绪/语气',state)
    dataset,export=frozen.save();metrics=evaluate(dataset,export)['report']['overall']
    assert metrics['structural_required_evaluations']==10 and metrics['applicable_fields']==9
    assert metrics['correct_not_applicable']==0 and metrics['missed_not_applicable']==1
    assert metrics['applicability_unresolved']==1 and metrics['structural_assessment_success']['value']==.9
    assert metrics['exact_match_labelled']['denominator']==9


@pytest.mark.parametrize('missing', ['evidence','producer','actual_media_kind'])
def test_na_requires_actual_media_and_producer(frozen,missing):
    case,prediction=frozen.add();gold_na(case,('情绪/语气',))
    prediction_state(frozen,prediction,'情绪/语气','not_applicable',missing=missing)
    dataset,export=frozen.save();metrics=evaluate(dataset,export)['report']['per_field']['情绪/语气']
    assert metrics['correct_not_applicable']==0 and metrics['unverified_not_applicable_claims']==1
    assert metrics['structural_assessment_success']['numerator']==0
    assert metrics['producer_missing' if missing=='producer' else 'evidence_missing']==1


def test_observed_medical_label_on_na_gold_counts_false_positive(frozen):
    case,prediction=frozen.add('life-photo');gold_na(case,('疾病/症状',))
    dataset,export=frozen.save();report=evaluate(dataset,export)['report'];metrics=report['overall']
    assert metrics['medical_false_positive_cases']==1 and metrics['medical_false_positive_labels']==1
    assert metrics['incorrect_applicable_claims']==1 and metrics['applicability_errors']==1
    assert metrics['label_precision']['denominator']==10 and metrics['label_precision']['value']==.9
    assert metrics['exact_match_labelled']['value']==1. and metrics['structural_assessment_success']['value']==.9


@pytest.mark.parametrize('state', ['no_match','not_applicable'])
def test_medical_candidates_on_na_gold_are_not_hidden(frozen,state):
    case,prediction=frozen.add('life-photo');gold_na(case,('科室',))
    prediction_state(frozen,prediction,'科室',state,candidates=('invented medical department',))
    dataset,export=frozen.save();metrics=evaluate(dataset,export)['report']['overall']
    assert metrics['medical_candidate_false_positive_cases']==1
    assert metrics['correct_not_applicable']==0 and metrics['incorrect_applicable_claims']==1
    assert metrics['medical_false_positive_cases']==0


def test_na_wrong_observed_enters_candidate_precision_denominator(frozen):
    first,_=frozen.add('cal-positive',split='calibration')
    second,_=frozen.add('cal-na',split='calibration');gold_na(second,('情绪/语气',))
    dataset,export=frozen.save();report=evaluate(dataset,export,split='calibration')['report']
    candidate=next(row for row in propose_rules(report,minimum_groups=1)['candidates'] if row['field']=='情绪/语气')
    assert candidate['observed_denominator']==2 and candidate['independent_observed_groups']==2
    assert candidate['empirical_exact_precision']==.5
    assert 'empirical_exact_precision_below_candidate_gate' in candidate['reasons']
    assert not candidate['calibrated'] and not candidate['automatic_enablement']


def test_observed_unassessable_gold_blocks_candidate_instead_of_silent_filter(frozen):
    first,_=frozen.add('cal-positive',split='calibration')
    second,_=frozen.add('cal-no-gold',split='calibration')
    gold=next(g for g in second['gold'] if g['field']=='情绪/语气')
    gold.update(state='unassessable',complete_value_set=False,values=[])
    dataset,export=frozen.save();report=evaluate(dataset,export,split='calibration')['report']
    candidate=next(row for row in propose_rules(report,minimum_groups=1)['candidates'] if row['field']=='情绪/语气')
    assert 'unassessable_gold_for_runtime_eligible_observation' in candidate['reasons']


def test_bgm_na_medical_hallucinations_are_counted_without_eight_field_shrinkage(frozen):
    case,prediction=frozen.add('ordinary-bgm','audio')
    gold_na(case,('科室','器官/系统','疾病/症状','过程/状态'))
    prediction_state(frozen,prediction,'过程/状态','no_match',candidates=('invented medical process',))
    dataset,export=frozen.save();metrics=evaluate(dataset,export)['report']['overall']
    assert metrics['structural_required_evaluations']==8 and metrics['applicable_fields']==4
    assert metrics['medical_false_positive_cases']==3 and metrics['medical_false_positive_labels']==3
    assert metrics['medical_candidate_false_positive_cases']==1
    assert metrics['correct_not_applicable']==2 and metrics['incorrect_applicable_claims']==4
    assert metrics['label_precision']['numerator']==4 and metrics['label_precision']['denominator']==7
    assert metrics['structural_assessment_success']=={'numerator':4,'denominator':8,'value':.5}
