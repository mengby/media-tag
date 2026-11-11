from datetime import datetime,timezone
from pathlib import Path
import json
import time

from media_tag.analysis.contracts import AnalysisDocument,MEDICAL_FIELDS,TOPIC_FIELDS
from media_tag.storage import StorageError
from .io import Bundle,EvaluationError,load,publish,strict_json
from .models import Dataset,Export
from .metrics import aggregate,latency


def _field(case,gold,item,*,evidence_missing=False,producer_missing=False,failure=None):
    applicable=gold.state!='not_applicable';scorable=gold.state=='labelled'
    structural=not(case.content_kind=='audio' and gold.field in ('画面主体','表现形式'))
    state=item.value.state if item else 'failed'
    values=set(item.value.values or ()) if item and state=='observed' else set()
    candidates=set(item.candidates) if item else set()
    expected=set(gold.values);verified=state=='observed' and not evidence_missing and not producer_missing
    verified_na=state=='not_applicable' and not candidates and not evidence_missing and not producer_missing
    correct_na=not applicable and verified_na
    incorrect_na=scorable and state=='not_applicable'
    incorrect_applicable=not applicable and (state=='observed' or bool(candidates))
    exact=bool(scorable and verified and values==expected)
    intersection=len(values&expected) if verified and scorable else 0
    union=values|expected
    extra=values-expected
    return {'case_id':case.case_id,'source_sha256':case.source_sha256,'near_group':case.near_group,
        'content_kind':case.content_kind,'field':gold.field,'structural_required':structural,
        'gold_state':gold.state,'annotation_note':gold.annotation_note,'applicable':applicable,'scorable':scorable,
        'state':state,'reason':failure or (item.reason if item else 'prediction_missing'),
        'next_step':item.next_step if item else 'provider','predicted_values':sorted(values),
        'candidate_values':sorted(candidates),'expected_values':list(gold.values),
        'verified_observation':verified,'verified_not_applicable_claim':verified_na,
        'correct_not_applicable':correct_na,'incorrect_not_applicable':incorrect_na,
        'incorrect_applicable_claim':incorrect_applicable,
        'applicability_correct':bool(correct_na or (scorable and verified)),
        'applicability_error':bool(incorrect_na or incorrect_applicable),
        'assessment_success':bool(exact or correct_na),'exact_success':exact,
        'true_positive_labels':intersection,'predicted_labels':len(values),'expected_labels':len(expected),
        'jaccard':intersection/len(union) if union else 0.,'evidence_missing':evidence_missing,
        'producer_missing':producer_missing,'negative_abstention':scorable and not expected and state in ('unknown','no_match'),
        'medical_false_positive':gold.field in MEDICAL_FIELDS and bool(extra),
        'medical_false_positive_labels':len(extra) if gold.field in MEDICAL_FIELDS else 0,
        'medical_candidate_false_positive':gold.field in MEDICAL_FIELDS and bool(candidates-expected),
        'direction_error':bool(values&set(gold.opposite_direction_values)),
        'candidate_direction_error':bool(candidates&set(gold.opposite_direction_values)),
        'forbidden_claim':bool((values|candidates)&set(gold.forbidden_values))}


def _document(bundle,prediction,case,run):
    try:
        raw=bundle.read(prediction.document);strict_json(raw)
        document=AnalysisDocument.model_validate_json(raw)
    except (StorageError,OSError,ValueError,TypeError):
        return None,'document_unavailable_or_invalid'
    if document.content_sha256!=case.source_sha256 or document.content_kind!=case.content_kind:
        raise EvaluationError('document_source_or_modality_changed')
    for item in document.fields:
        if item.versions is not None and not any(pin.versions==item.versions for pin in run.producer_pins):
            raise EvaluationError('field_model_rules_preprocess_taxonomy_pin_changed')
        for key in item.producer_ids:
            producer=next(p for p in document.producers if p.producer_id==key)
            if not any(pin.versions==producer.versions and pin.prompt_version==producer.prompt_version for pin in run.producer_pins):
                raise EvaluationError('producer_prompt_pin_changed')
    return document,None


def evaluate(dataset_path,export_path,*,split='acceptance',output_directory=None):
    """Read frozen private imports; never invokes models, APIs or production databases."""
    started=time.perf_counter()
    dataset,dataset_sha=load(dataset_path,Dataset);export,export_sha=load(export_path,Export)
    if split not in ('calibration','acceptance'):raise EvaluationError('calibration_or_acceptance_only')
    if export.split_scope is not None and export.split_scope!=split:raise EvaluationError('export_split_scope_changed')
    if export.dataset_sha256!=dataset_sha or export.purpose!=dataset.purpose:
        raise EvaluationError('export_dataset_or_purpose_changed')
    if export.taxonomy_version!=dataset.taxonomy_version or export.taxonomy_sha256!=dataset.taxonomy_sha256:
        raise EvaluationError('published_taxonomy_binding_changed')
    if export.exported_at<dataset.frozen_at:raise EvaluationError('prediction_export_predates_frozen_dataset')
    run=next((r for r in dataset.runs if r.run_id==export.run_id),None)
    if run is None:raise EvaluationError('unfrozen_run')
    cases={c.case_id:c for c in dataset.cases};predictions={p.case_id:p for p in export.predictions}
    for prediction in export.predictions:
        case=cases.get(prediction.case_id)
        if case is None or case.source_sha256!=prediction.source_sha256 or case.content_kind!=prediction.content_kind:
            raise EvaluationError('prediction_case_source_or_modality_changed')
        if run.case_ids is not None and prediction.case_id not in run.case_ids:
            raise EvaluationError('prediction_outside_frozen_run_scope')
    selected=[case for case in dataset.cases if case.split==split and (run.case_ids is None or case.case_id in run.case_ids)]
    if not selected:raise EvaluationError('selected_split_empty')
    bundle=Bundle(Path(export_path).parent)
    rows=[];case_results=[]
    for case in selected:
        prediction=predictions.get(case.case_id);document=None;failure=None
        if prediction is None:failure='prediction_missing'
        elif prediction.failure_code is not None:failure=prediction.failure_code
        else:document,failure=_document(bundle,prediction,case,run)
        for gold in case.gold:
            item=next((f for f in document.fields if f.field==gold.field),None) if document else None
            evidence_missing=failure=='document_unavailable_or_invalid';producer_missing=False
            if item and (item.value.state in ('observed','not_applicable') or item.candidates):
                evidence_missing=not item.evidence_ids
                if item.value.state=='not_applicable':
                    actual_kind={'image':'image','audio':'audio_segment','video':'video_segment','animation':'video_segment'}[case.content_kind]
                    if not any(e.evidence_id in item.evidence_ids and e.kind==actual_kind for e in document.evidence):
                        evidence_missing=True
                for evidence in document.evidence:
                    if evidence.evidence_id in item.evidence_ids:
                        try:bundle.read(evidence.artifact)
                        except (StorageError,OSError,ValueError):evidence_missing=True
                producer_missing=not item.producer_ids
                for producer in document.producers:
                    if producer.producer_id in item.producer_ids:
                        try:bundle.read(producer.report)
                        except (StorageError,OSError,ValueError):producer_missing=True
            rows.append(_field(case,gold,item,evidence_missing=evidence_missing,producer_missing=producer_missing,failure=failure))
        case_results.append({'case_id':case.case_id,'source_sha256':case.source_sha256,'near_group':case.near_group,
            'content_kind':case.content_kind,'failure':failure,'declared_pipeline_elapsed_seconds':prediction.elapsed_seconds if prediction else None,
            'elapsed_timing_scope':prediction.timing_scope if prediction else 'not_measured'})
    grouped={}
    for row in rows:grouped.setdefault((row['content_kind'],row['field']),[]).append(row)
    measured_scopes=sorted({c['elapsed_timing_scope'] for c in case_results if c['declared_pipeline_elapsed_seconds'] is not None})
    global_latency=latency([c['declared_pipeline_elapsed_seconds'] for c in case_results],
        scope=measured_scopes[0] if len(measured_scopes)==1 else 'declared_pipeline_elapsed')
    if len(measured_scopes)>1:
        global_latency.update(sum_seconds=None,p50_seconds=None,p95_seconds=None,
            method='not_aggregated_mixed_timing_scopes',reason='mixed_timing_scopes',timing_scopes=measured_scopes)
    report={'schema_version':'ct-topic-evaluation-report-v1','dataset_id':dataset.dataset_id,'dataset_sha256':dataset_sha,
        'export_sha256':export_sha,'run':run.model_dump(mode='json'),'purpose':dataset.purpose,'split':split,
        'generated_at':datetime.now(timezone.utc).isoformat(),'calibrated':False,'automatic_enablement':False,
        'business_acceptance':False,'training_membership_declared_complete':dataset.training_membership_declared_complete,
        'annotation_provenance':{'annotators':dataset.annotators,'adjudicator':dataset.adjudicator,
            'independently_human_annotated':dataset.independently_human_annotated,
            'reference':dataset.annotation_reference,'frozen_at':dataset.frozen_at.isoformat(),
            'identity_verification':'not_performed_by_tool'},
        'split_case_counts':{s:sum(c.split==s for c in dataset.cases) for s in ('training','calibration','acceptance')},
        'taxonomy_version':dataset.taxonomy_version,'taxonomy_sha256':dataset.taxonomy_sha256,
        'evaluated_case_scope':[case.case_id for case in selected],
        'overall':aggregate(rows),'per_field':{field:aggregate([r for r in rows if r['field']==field]) for field in TOPIC_FIELDS},
        'per_modality':{kind:aggregate([r for r in rows if r['content_kind']==kind]) for kind in sorted({r['content_kind'] for r in rows})},
        'per_field_modality':[{'content_kind':kind,'field':field,'metrics':aggregate(values)} for (kind,field),values in sorted(grouped.items())],
        'latency':global_latency,
        'latency_by_scope':{scope:latency([c['declared_pipeline_elapsed_seconds'] for c in case_results if c['elapsed_timing_scope']==scope],scope=scope)
            for scope in sorted({c['elapsed_timing_scope'] for c in case_results})},
        'cases':case_results,'fields':rows,'evaluation_elapsed_seconds':time.perf_counter()-started,
        'assumptions':['Only declared near groups/project training membership are checked; public pretraining membership is unknown.',
            'Annotation identity, independence, exhaustive gold values and source grouping require human verification.',
            'Bounded exported document/evidence/producer bytes are hashed; raw source bytes are not re-decoded and current Router permissions/state are not queried.',
            'Exact/set scores use literal frozen labels; no inferred synonyms or semantic direction guessing.',
            'Unknown/no_match/failed/not_applicable on applicable fields never count as exact success, even for empty expected sets.',
            'Visual ten/audio eight structural required evaluations are separate from independently annotated field applicability; verified correct NA is scored separately.',
            'Sample-weighted metrics are correlated within near groups; declared group counts are shown, no statistical confidence is claimed.',
            'Pipeline timing is explicitly imported, including failures; evaluation timing is measured locally, neither proves model cold-start latency.']}
    if output_directory is not None:
        path,digest=publish(output_directory,report)
        return {'report':report,'path':path,'sha256':digest}
    return {'report':report,'path':None,'sha256':None}
