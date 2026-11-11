from collections import Counter
from media_tag.terminology.evaluation.metrics import fraction,percentile


def aggregate(rows):
    applicable=[r for r in rows if r['applicable']]
    scored=[r for r in applicable if r['scorable']]
    na=[r for r in rows if not r['applicable']]
    required=[r for r in rows if r['structural_required']]
    known=scored+na
    tp=sum(r['true_positive_labels'] for r in scored)
    predicted=sum(r['predicted_labels'] for r in known)
    expected=sum(r['expected_labels'] for r in scored)
    correct=sum(r['exact_success'] for r in scored)
    return {'total_field_assessments':len(rows),'structural_required_evaluations':len(required),
        'structural_assessment_success':fraction(sum(r['assessment_success'] for r in required),len(required)),
        'applicable_fields':len(applicable),'labelled_fields':len(scored),
        'unassessable_fields':len(applicable)-len(scored),'not_applicable_fields':len(rows)-len(applicable),
        'additional_not_applicable_fields':sum(r['structural_required'] for r in na),
        'correct_not_applicable':sum(r['correct_not_applicable'] for r in na),
        'incorrect_not_applicable':sum(r['incorrect_not_applicable'] for r in rows),
        'incorrect_applicable_claims':sum(r['incorrect_applicable_claim'] for r in na),
        'missed_not_applicable':sum(not r['correct_not_applicable'] for r in na),
        'unverified_not_applicable_claims':sum(r['state']=='not_applicable' and not r['verified_not_applicable_claim'] for r in rows),
        'applicability_accuracy_required':fraction(sum(r['applicability_correct'] for r in required),len(required)),
        'applicability_errors':sum(r['applicability_error'] for r in rows),
        'applicability_unresolved':sum(not r['applicability_correct'] and not r['applicability_error'] for r in rows),
        'observed_claims':sum(r['state']=='observed' for r in rows),
        'verified_observed_coverage':fraction(sum(r['verified_observation'] for r in applicable),len(applicable)),
        'exact_match_labelled':fraction(correct,len(scored)),
        'end_to_end_exact_coverage':fraction(correct,len(applicable)),
        'label_precision':fraction(tp,predicted),'label_recall':fraction(tp,expected),
        'micro_f1':fraction(2*tp,predicted+expected),
        'mean_jaccard':fraction(sum(r['jaccard'] for r in scored),len(scored)),
        'abstentions':sum(r['state'] in ('unknown','no_match') or (r['applicable'] and r['state']=='not_applicable') for r in rows),
        'failures':sum(r['state']=='failed' for r in rows),
        'evidence_missing':sum(r['evidence_missing'] for r in rows),
        'producer_missing':sum(r['producer_missing'] for r in rows),
        'appropriate_negative_abstentions':sum(r['negative_abstention'] for r in scored),
        'medical_false_positive_cases':sum(r['medical_false_positive'] for r in known),
        'medical_false_positive_labels':sum(r['medical_false_positive_labels'] for r in known),
        'medical_candidate_false_positive_cases':sum(r['medical_candidate_false_positive'] for r in known),
        'direction_error_cases':sum(r['direction_error'] for r in known),
        'candidate_direction_error_cases':sum(r['candidate_direction_error'] for r in known),
        'forbidden_claim_cases':sum(r['forbidden_claim'] for r in known),
        'state_counts':dict(Counter(r['state'] for r in rows)),
        'next_step_counts':dict(Counter(r['next_step'] for r in rows)),
        'reason_counts':dict(Counter(r['reason'] for r in rows)),
        'independent_declared_groups':len({r['near_group'] for r in rows})}


def latency(values,*,scope='declared_pipeline_elapsed'):
    values=[v for v in values if v is not None]
    return {'measured_cases':len(values),'sum_seconds':sum(values),'p50_seconds':percentile(values,.5),
            'p95_seconds':percentile(values,.95),'method':f'nearest_rank_{scope}_including_failures'}
