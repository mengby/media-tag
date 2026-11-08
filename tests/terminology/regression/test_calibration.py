from array import array
from hashlib import sha256
import json
import sys
import pytest
from media_tag.catalog.service import encode
from media_tag.terminology.evaluation.calibration import frozen_index,calibrate,grid
from media_tag.terminology.evaluation.runner import EvaluationError
from .test_metrics import dataset,example


def index(tmp_path,data):
    documents=encode([{'concept_id':'circle'}]).encode();packed=array('f',[1.,*([0.]*511)])
    if sys.byteorder!='little':packed.byteswap()
    metadata=dict(schema_version='d14-text-index-v1',taxonomy_release_id=data.pin.taxonomy_release_id,lexicon_release_id=data.pin.lexicon_release_id,
        model_revision=data.pin.model_revision,preprocess_version=data.pin.preprocess_version,dimension=512,rows=1,documents_sha256=sha256(documents).hexdigest())
    values={'documents.json':documents,'vectors.f32':packed.tobytes(),'index.json':encode(metadata).encode()}
    for name,raw in values.items():(tmp_path/name).write_bytes(raw)
    return {name:sha256(raw).hexdigest() for name,raw in values.items()}


def test_frozen_calibration_rejects_tampered_index_and_evaluation_split(tmp_path):
    data=dataset();bundle=index(tmp_path,data)
    documents,vectors=frozen_index(tmp_path,bundle,data.pin)
    assert documents[0]['concept_id']=='circle' and len(vectors[0])==512
    (tmp_path/'documents.json').write_bytes(b'[]')
    with pytest.raises(EvaluationError,match='hash_changed'):frozen_index(tmp_path,bundle,data.pin)
    with pytest.raises(EvaluationError,match='cannot_tune'):grid(data.model_copy(update={'split':'evaluation'}),[])


def test_threshold_grid_does_not_enable_auto_resolution_or_tune_on_failure(tmp_path):
    data=dataset();bundle=index(tmp_path,data)
    class SyntheticEncoder:
        stats={'synthetic_protocol_only':True}
        def encode(self,*args,**kwargs):return [[1.,*([0.]*511)]]
    result=calibrate(data,tmp_path,bundle,SyntheticEncoder(),data.pin.model_revision)
    assert result['recommended_min_cosine']==.95 and result['model_candidates_still_require_confirmation']
    assert not result['thresholds_changed'] and not result['automatic_resolution_measured'] and not result['business_acceptance']
    failed=grid(data,[{'case_id':'one','ranked':[],'failure':'timeout'}])
    assert failed['recommended_min_cosine'] is None and failed['inference_failures']==1


def test_unknown_false_candidate_blocks_recommendation():
    data=dataset(cases=[example().model_dump(mode='json'),example(case_id='unknown',source_group='unknown-group',query='Unknown input',category='out_of_scope',expectation='reject',acceptable_concepts=(),allowed_automatic_concepts=(),relevant_asset_ids=()).model_dump(mode='json')])
    rows=[{'case_id':case.case_id,'ranked':[{'concept_id':'circle','score':1.0}],'failure':None} for case in data.cases]
    assert grid(data,rows)['recommended_min_cosine'] is None
