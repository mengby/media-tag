from media_tag.analysis.contracts import MEDICAL_FIELDS
from .io import EvaluationError


def propose_rules(report,*,minimum_groups=5,minimum_exact_precision=.99):
    """Empirical calibration bucket candidates, never per-item model confidence."""
    if report['split']!='calibration':raise EvaluationError('acceptance_must_not_tune_rules')
    if type(minimum_groups) is not int or minimum_groups<1:raise ValueError('positive_group_threshold_required')
    if type(minimum_exact_precision) not in (int,float) or not 0<minimum_exact_precision<=1:
        raise ValueError('precision_threshold_required')
    candidates=[]
    for bucket in report['per_field_modality']:
        metrics=bucket['metrics']
        runtime_eligible=[row for row in report['fields'] if row['field']==bucket['field']
            and row['content_kind']==bucket['content_kind'] and row['verified_observation']
            and not row['candidate_values']]
        eligible=[row for row in runtime_eligible if row['scorable'] or row['gold_state']=='not_applicable']
        denominator=len(eligible)
        correct=sum(row['exact_success'] for row in eligible)
        empirical=correct/denominator if denominator else None
        observed_groups=len({row['near_group'] for row in eligible})
        reasons=[]
        if len(eligible)!=len(runtime_eligible):reasons.append('unassessable_gold_for_runtime_eligible_observation')
        if bucket['field'] in MEDICAL_FIELDS:reasons.append('medical_fields_always_require_human_review')
        if report['purpose']!='business':reasons.append('synthetic_only')
        if not report['training_membership_declared_complete']:reasons.append('project_training_scope_incomplete')
        if observed_groups<minimum_groups:reasons.append('insufficient_independent_observed_groups')
        if empirical is None or empirical<minimum_exact_precision:reasons.append('empirical_exact_precision_below_candidate_gate')
        if (metrics['evidence_missing'] or metrics['producer_missing'] or metrics['failures']
                or metrics['direction_error_cases'] or metrics['candidate_direction_error_cases'] or metrics['forbidden_claim_cases']):
            reasons.append('integrity_failure_or_direction_error')
        candidates.append({'field':bucket['field'],'content_kind':bucket['content_kind'],
            'status':'blocked' if reasons else 'candidate_requires_independent_acceptance_and_human_approval',
            'reasons':reasons,'empirical_exact_precision':empirical,'observed_denominator':denominator,
            'independent_observed_groups':observed_groups,
            'minimum_groups':minimum_groups,'minimum_exact_precision':minimum_exact_precision,
            'runtime_preconditions':['observed_value_only','no_unmatched_candidates','current_frozen_producer_and_vocabulary',
                                     'complete_verified_evidence','non_medical_field'],
            'keep_non_observed_in_precision':True,
            'automatic_enablement':False,'calibrated':False})
    return {'schema_version':'ct-topic-rule-candidates-v1','dataset_sha256':report['dataset_sha256'],
        'export_sha256':report['export_sha256'],'purpose':report['purpose'],'run':report['run'],
        'candidate_basis':'empirical_field_modality_buckets_not_model_self_confidence','candidates':candidates,
        'calibrated':False,'automatic_enablement':False,'business_acceptance':False}
