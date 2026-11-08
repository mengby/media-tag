import json
import pytest
from media_tag.terminology.evaluation.models import Case,Criteria,Dataset
from media_tag.terminology.evaluation.metrics import score,report,percentile
from media_tag.terminology.evaluation.runner import validate_split,EvaluationError,Client,NoRedirect,load_dataset,compare


def example(**changes):
    values=dict(case_id='one',source_group='group-one',category='common',query='Synthetic circle',expectation='clear',
        acceptable_concepts=('circle',),allowed_automatic_concepts=('circle',),relevant_asset_ids=('asset-circle',),
        annotation_note='Synthetic protocol only')
    return Case(**(values|changes))


def dataset(**changes):
    values=dict(dataset_id='synthetic-protocol',split='calibration',purpose='synthetic_engineering',frozen_at='2026-11-08T00:00:00Z',
        annotators=['synthetic-fixture'],adjudicator='synthetic-protocol-only',annotation_reference='Synthetic responses, not human business annotations',
        independently_human_annotated=False,pin=dict(taxonomy_release_id='tax',lexicon_release_id='lex',embedding_index_id='index',
            model_revision='0'*64,preprocess_version='preprocess',match_version='match',resolution_policy_sha256='0'*64),candidate_pool_sha256='0'*64,cases=[example().model_dump(mode='json')])
    return Dataset.model_validate_json(json.dumps(values|changes))


def response(**changes):
    return dict(resolution={'state':'resolved','model_state':'not_needed','candidates':[{'concept_id':'circle'}]},
        selected_concept_ids=['circle'],intent={'polarity':'affirmed','direction':'unspecified'},items=[{'versions':[{'asset_id':'asset-circle'}]}])|changes


def test_all_rejection_is_not_perfect_accuracy_and_failures_stay_in_denominators():
    case=example();rows=[score(case,response(resolution={'state':'no_match'},selected_concept_ids=[],items=[]),elapsed_seconds=.1),
        score(case,None,elapsed_seconds=30,error='transport_timeout')]
    result=report(rows,Criteria())
    assert result['precision']['value'] is None and result['common_coverage']['value']==0
    assert result['concept_recall5']=={'macro_mean':0,'queries':2}
    assert result['asset_hit10']=={'numerator':0,'denominator':2,'value':0}
    assert result['rejection_rate']['numerator']==1 and result['failure_rate']['numerator']==1
    assert result['latency_seconds']['p95']==30 and not result['proposed_metric_gates_passed']


def test_macro_recall_hit_and_complete_recall_have_different_denominators():
    first=example(acceptable_concepts=('circle','square'),allowed_automatic_concepts=('circle','square'),
        relevant_asset_ids=('asset-circle','asset-square'),complete_relevant_set=True)
    second=example(case_id='two',category='out_of_scope',expectation='reject',acceptable_concepts=(),allowed_automatic_concepts=(),relevant_asset_ids=())
    result=report([score(first,response(),elapsed_seconds=.1),score(second,response(resolution={'state':'no_match'},selected_concept_ids=[],items=[]),elapsed_seconds=.2)],Criteria())
    assert result['concept_recall5']['macro_mean']==.5 and result['concept_recall5']['queries']==1
    assert result['asset_hit10']['denominator']==1 and result['asset_hit10']['value']==1
    assert result['asset_recall10']['macro_mean']==.5
    assert result['queries_without_visible_relevant_assets']==1
    assert percentile([1,2,3,4,5],.95)==5


def test_direction_wrong_disease_and_forbidden_asset_are_not_hidden_by_average():
    case=example(expected_direction='decreasing',forbidden_concepts=('disease',),forbidden_asset_ids=('wrong-asset',))
    row=score(case,response(selected_concept_ids=['disease'],intent={'polarity':'affirmed','direction':'increasing'},items=[{'versions':[{'asset_id':'wrong-asset'}]}]),elapsed_seconds=.1)
    result=report([row],Criteria(precision=0,common_coverage=0,concept_recall5=0,asset_hit10=0))
    assert result['direction_errors']==1 and result['forbidden_concept_expansions']==1 and result['forbidden_asset_hits']==1
    assert not result['proposed_metric_gates_passed']
    assert report([row],Criteria(),semantic=False)['precision'] is None


def test_split_checks_source_groups_and_normalized_queries(tmp_path):
    calibration=dataset();path=tmp_path/'calibration.json';raw=calibration.model_dump_json().encode();path.write_bytes(raw)
    from hashlib import sha256
    evaluation=dataset(split='evaluation',calibration_sha256=sha256(raw).hexdigest(),cases=[example(case_id='other',source_group='other-group',query='Another synthetic query').model_dump(mode='json')])
    assert load_dataset(path)[1]==sha256(raw).hexdigest()
    validate_split(evaluation,calibration,sha256(raw).hexdigest())
    with pytest.raises(EvaluationError,match='separate_calibration'):compare(None,evaluation)
    with pytest.raises(EvaluationError,match='source_group'):validate_split(evaluation.model_copy(update={'cases':(example(query='Another query'),)}),calibration,sha256(raw).hexdigest())
    with pytest.raises(EvaluationError,match='query_leakage'):validate_split(evaluation.model_copy(update={'cases':(example(source_group='different'),)}),calibration,sha256(raw).hexdigest())
    with pytest.raises(EvaluationError,match='criteria'):validate_split(evaluation.model_copy(update={'criteria':Criteria(precision=.5)}),calibration,sha256(raw).hexdigest())


def test_annotations_and_transport_do_not_accept_silent_changes():
    with pytest.raises(ValueError):example(category='common',expectation='reject',allowed_automatic_concepts=())
    with pytest.raises(ValueError):example(allowed_automatic_concepts=('unknown',))
    with pytest.raises(ValueError):dataset(purpose='business')
    with pytest.raises(EvaluationError):Client('http://untrusted.example','synthetic-token')
    with pytest.raises(EvaluationError):Client('https://user:password@example.com','synthetic-token')
    with pytest.raises(EvaluationError,match='redirect_forbidden'):NoRedirect().redirect_request(None,None,None,None,None,None)


def test_duplicate_json_keys_and_degraded_lexical_fallback_are_not_misreported(tmp_path):
    path=tmp_path/'duplicate.json';path.write_text('{"dataset_id":"one","dataset_id":"two"}')
    with pytest.raises(EvaluationError,match='duplicate_dataset_key'):load_dataset(path)
    value=response(resolution={'state':'degraded','model_state':'timed_out'})
    assert score(example(),value,elapsed_seconds=1)['failure']=='text_model_timeout'
    assert score(example(),value,elapsed_seconds=1,semantic=False)['failure'] is None


def test_comparison_binds_versions_and_pool_and_cannot_claim_business_acceptance():
    from media_tag.catalog.service import encode
    from hashlib import sha256
    records=[dict(asset_id='asset-circle',revision=1,sha256='1'*64)]
    data=dataset(candidate_pool_sha256=sha256(encode(records).encode()).hexdigest())
    class Protocol:
        changed=False;drift=False;queried=False
        def call(self,path,payload=None):
            if path=='/search/vocabulary':return {'versions':data.pin.model_dump(mode='json'),'concepts':[{'concept_id':'circle'}]}
            if path.startswith('/ui/library'):return {'items':[{'versions':[records[0]|{'file_access':'authorized','revision':2 if self.drift and self.queried else 1}]}],'next':None}
            self.queried=True
            result=response();result['resolution']['versions']=data.pin.model_dump(mode='json')
            if self.changed:result['resolution']['versions']['lexicon_release_id']='other'
            return result
    protocol=Protocol();result=compare(protocol,data)
    assert not result['business_eligible'] and not result['business_acceptance']
    assert result['metrics']['lexicon_bge']['asset_hit10']['value']==1
    protocol.changed=True
    with pytest.raises(EvaluationError,match='semantic_version_changed'):compare(protocol,data)
    protocol.changed=False;protocol.drift=True;protocol.queried=False
    with pytest.raises(EvaluationError,match='candidate_pool_changed'):compare(protocol,data)
