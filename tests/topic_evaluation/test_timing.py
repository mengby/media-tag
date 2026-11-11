import json

import pytest
from media_tag.topic_evaluation import Export,evaluate
from media_tag.topic_evaluation.models import Prediction


def legacy_prediction():
    return {'case_id':'synthetic-legacy','source_sha256':'a'*64,'content_kind':'image',
            'failure_code':'synthetic_not_measured'}


def test_legacy_python_prediction_defaults_to_unmeasured_without_mutating_input():
    data=legacy_prediction();prediction=Prediction(**data)
    assert prediction.elapsed_seconds is None and prediction.timing_scope=='not_measured'
    assert 'timing_scope' not in data


def test_legacy_json_prediction_and_export_without_timing_fields_are_compatible():
    data=legacy_prediction()
    prediction=Prediction.model_validate_json(json.dumps(data))
    assert prediction.elapsed_seconds is None and prediction.timing_scope=='not_measured'
    exported=Export.model_validate_json(json.dumps({'purpose':'synthetic_engineering','dataset_sha256':'b'*64,
        'run_id':'synthetic-run','exported_at':'2026-01-02T00:00:00Z','predictions':[data]}))
    assert exported.predictions[0].timing_scope=='not_measured'


@pytest.mark.parametrize('scope,elapsed', [('declared_pipeline_elapsed',None),
    ('sum_verified_producer_stage_seconds',None),('not_measured',0.)])
def test_explicit_contradictory_timing_is_still_rejected(scope,elapsed):
    with pytest.raises(ValueError,match='explicit_unmeasured_timing_or_actual_elapsed_required'):
        Prediction(**legacy_prediction(),elapsed_seconds=elapsed,timing_scope=scope)


def test_mixed_measured_scopes_do_not_produce_global_quantiles(frozen):
    frozen.add('pipeline',elapsed=2.);frozen.add('stages',elapsed=20.);frozen.add('unmeasured',elapsed=1.)
    frozen.predictions[1]['timing_scope']='sum_verified_producer_stage_seconds'
    frozen.predictions[2].pop('elapsed_seconds')
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    global_timing=report['latency']
    assert global_timing['measured_cases']==2
    assert global_timing['p50_seconds'] is None and global_timing['p95_seconds'] is None
    assert global_timing['sum_seconds'] is None and global_timing['reason']=='mixed_timing_scopes'
    assert global_timing['timing_scopes']==['declared_pipeline_elapsed','sum_verified_producer_stage_seconds']
    scopes=report['latency_by_scope']
    assert scopes['declared_pipeline_elapsed']['p95_seconds']==2.
    assert scopes['sum_verified_producer_stage_seconds']['p95_seconds']==20.
    assert scopes['not_measured']['measured_cases']==0 and scopes['not_measured']['p95_seconds'] is None


def test_single_measured_scope_keeps_quantiles_when_other_cases_are_unmeasured(frozen):
    frozen.add('measured',elapsed=3.);frozen.add('unmeasured',elapsed=1.)
    frozen.predictions[1].pop('elapsed_seconds')
    dataset,export=frozen.save();report=evaluate(dataset,export)['report']
    assert report['latency']=={'measured_cases':1,'sum_seconds':3.,'p50_seconds':3.,'p95_seconds':3.,
        'method':'nearest_rank_declared_pipeline_elapsed_including_failures'}
