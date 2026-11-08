"""Offline model-component calibration on a separately frozen calibration split only."""
from array import array
from hashlib import sha256
import json
import math
from pathlib import Path
import sys
import time
from media_tag.terminology.embedding.index import checked_vectors
from .metrics import fraction,percentile
from .runner import EvaluationError


def frozen_index(directory,bundle,pin):
    expected={'documents.json','index.json','vectors.f32'}
    if set(bundle)!=expected:raise EvaluationError('exact_index_checksum_set_required')
    values={}
    for name,digest in bundle.items():
        path=Path(directory)/name
        if path.is_symlink():raise EvaluationError('index_symlink_forbidden')
        with path.open('rb') as stream:raw=stream.read(32*1024**2+1)
        if len(raw)>32*1024**2 or sha256(raw).hexdigest()!=digest:raise EvaluationError('frozen_index_hash_changed')
        values[name]=raw
    metadata=json.loads(values['index.json']);documents=json.loads(values['documents.json'])
    expected_metadata={'schema_version':'d14-text-index-v1','taxonomy_release_id':pin.taxonomy_release_id,'lexicon_release_id':pin.lexicon_release_id,
        'model_revision':pin.model_revision,'preprocess_version':pin.preprocess_version,'dimension':512,
        'documents_sha256':sha256(values['documents.json']).hexdigest(),'rows':len(documents)}
    if metadata!=expected_metadata or not 1<=len(documents)<=5000 or len(values['vectors.f32'])!=len(documents)*512*4:
        raise EvaluationError('frozen_index_shape_or_version')
    if len({d['concept_id'] for d in documents})!=len(documents):raise EvaluationError('duplicate_concept_documents')
    packed=array('f');packed.frombytes(values['vectors.f32'])
    if sys.byteorder!='little':packed.byteswap()
    vectors=checked_vectors([list(packed[i*512:(i+1)*512]) for i in range(len(documents))],len(documents))
    return documents,vectors


def grid(dataset,observations):
    if dataset.split!='calibration':raise EvaluationError('evaluation_cannot_tune_thresholds')
    if {r['case_id'] for r in observations}!={c.case_id for c in dataset.cases} or len(observations)!=len(dataset.cases):raise EvaluationError('calibration_observation_coverage')
    byid={r['case_id']:r for r in observations};results=[];failures=sum(bool(r['failure']) for r in observations)
    for threshold in (.5,.55,.6,.65,.7,.75,.8,.85,.9,.95):
        accepted=correct=common=known=bad_unknown=0;recalls=[]
        for case in dataset.cases:
            row=byid[case.case_id];ranked=[r for r in row['ranked'] if r['score']>=threshold] if not row['failure'] else []
            accepted+=bool(ranked);correct+=bool(ranked and ranked[0]['concept_id'] in case.acceptable_concepts)
            if case.category=='common':known+=1;common+=bool(ranked)
            if case.expectation=='reject' and ranked:bad_unknown+=1
            if case.acceptable_concepts:recalls.append(len({r['concept_id'] for r in ranked[:5]}&set(case.acceptable_concepts))/len(case.acceptable_concepts))
        precision=fraction(correct,accepted);coverage=fraction(common,known)
        results.append({'min_cosine':threshold,'top1_candidate_precision':precision,'common_candidate_coverage':coverage,
            'concept_recall5_macro':sum(recalls)/len(recalls) if recalls else None,'out_of_scope_candidates':bad_unknown,
            'proposed_gate':precision['value'] is not None and precision['value']>=dataset.criteria.precision
                and coverage['value'] is not None and coverage['value']>=dataset.criteria.common_coverage and not bad_unknown and not failures})
    eligible=[r for r in results if r['proposed_gate']]
    recommendation=max(eligible,key=lambda r:(r['concept_recall5_macro'] or 0,r['min_cosine']))['min_cosine'] if eligible else None
    # Margin is an ambiguity hint only in D14; it must never authorize a model target.
    hints=[]
    for margin in (0,.03,.05,.1,.15):
        predicted=correct=0
        for case in dataset.cases:
            row=byid[case.case_id];ranked=[r for r in row['ranked'] if recommendation is not None and r['score']>=recommendation] if not row['failure'] else []
            ambiguous=len(ranked)>1 and ranked[0]['score']-ranked[1]['score']<margin
            predicted+=ambiguous;correct+=ambiguous and case.expectation=='clarify'
        hints.append({'min_margin':margin,'ambiguity_hint_precision':fraction(correct,predicted)})
    return {'threshold_grid':results,'recommended_min_cosine':recommendation,'margin_hint_grid':hints,'inference_failures':failures,
        'thresholds_changed':False,'model_candidates_still_require_confirmation':True,'automatic_resolution_measured':False,
        'calibration_scope':'BGE model component, not lexical routing or end-to-end retrieval; independently held-out evaluation still required'}


def calibrate(dataset,directory,bundle,encoder,model_revision):
    if dataset.split!='calibration':raise EvaluationError('evaluation_cannot_tune_thresholds')
    if model_revision!=dataset.pin.model_revision:raise EvaluationError('calibration_model_changed')
    documents,vectors=frozen_index(directory,bundle,dataset.pin)
    if len(dataset.cases)>500 or len(dataset.cases)*len(documents)>1000000:raise EvaluationError('calibration_compute_budget')
    ids={d['concept_id'] for d in documents}
    if any(not set(c.acceptable_concepts)<=ids for c in dataset.cases):raise EvaluationError('calibration_target_unavailable')
    observations=[]
    for case in dataset.cases:
        start=time.monotonic();failure=None;ranked=[]
        try:
            query=checked_vectors(encoder.encode([case.query],query=True,timeout_seconds=30),1)[0]
            ranked=sorted(({'concept_id':d['concept_id'],'score':max(-1,min(1,math.fsum(a*b for a,b in zip(query,v))))} for d,v in zip(documents,vectors)),key=lambda r:(-r['score'],r['concept_id']))[:5]
        except Exception as exc:
            from media_tag.models.contracts import ModelFailure
            failure=exc.code if isinstance(exc,ModelFailure) else 'calibration_inference_failed'
        observations.append({'case_id':case.case_id,'ranked':ranked,'failure':failure,'seconds':time.monotonic()-start})
    times=[r['seconds'] for r in observations]
    result=grid(dataset,observations)
    return result|{'observations':observations,'pin':dataset.pin.model_dump(mode='json'),'index_checksums':bundle,
        'model_executed':True,'resource_usage':getattr(encoder,'stats',{}),'initial_query_seconds':times[0] if times else None,
        'latency_seconds':{'samples_including_cold_and_errors':len(times),'p50':percentile(times,.5),'p95':percentile(times,.95)},
        'subsequent_latency_seconds':{'samples_including_errors':len(times[1:]),'p50':percentile(times[1:],.5),'p95':percentile(times[1:],.95)},
        'business_acceptance':False}
