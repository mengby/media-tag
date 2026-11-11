"""Deterministic provider reply adoption, shared by execution and verification."""
from media_tag.analysis.contracts import FieldAssessment, TOPIC_FIELDS, MEDICAL_FIELDS
from media_tag.contracts.models import FieldValue


def assessments(reply, task, policy, kind, *, layer='precision', evidence_id='precision-content'):
    if layer not in ('precision', 'local_audio'):
        raise ValueError('supported analysis layer required')
    fields = {field: FieldAssessment(field=field, value=FieldValue(state='unknown', reason='not_requested'),
        next_step='local', reason='not_requested') for field in TOPIC_FIELDS}
    if kind == 'audio':
        for field in ('画面主体', '表现形式'):
            fields[field] = FieldAssessment(field=field, value=FieldValue(state='not_applicable', reason='pure_audio'),
                next_step='none', reason='pure_audio')
    for item in reply.fields:
        value, candidates = item.value, item.candidates
        definition = policy.asset_fields[item.field]
        allowed = policy.options(definition)
        if value.state == 'observed' and definition.field_type in ('单选', '多选') and (allowed is None or not set(value.values) <= set(allowed)):
            candidates = tuple(dict.fromkeys((*candidates, *value.values)))
            value = FieldValue(state='no_match', reason='candidate_outside_published_options')
        # Model-proposed applicability is retained verbatim in report.reply. It
        # cannot become a local exemption before independent calibration/review.
        proposed_na = layer == 'local_audio' and value.state == 'not_applicable'
        if proposed_na:
            value = FieldValue(state='unknown', reason='local_audio_applicability_requires_review')
        policy.validate(definition, value)
        action = ('vocabulary' if candidates or value.state == 'no_match' else
            'none' if value.state in ('observed', 'not_applicable') else 'human')
        reason = 'precision_draft_requires_separate_human_review' if action == 'none' else 'precision_unresolved'
        if layer == 'local_audio':
            if action != 'vocabulary':
                action = 'human' if item.field in MEDICAL_FIELDS else 'precision'
            reason = 'local_audio_applicability_requires_review' if proposed_na else (
                'local_audio_uncalibrated_draft' if value.state == 'observed' else 'local_audio_unresolved')
        fields[item.field] = FieldAssessment(field=item.field, value=value, candidates=candidates,
            next_step=action, reason=reason, origin='local' if layer=='local_audio' else 'precision',
            versions=task.versions, evidence_ids=(evidence_id,), dynamic=item.dynamic)
    return tuple(fields[field] for field in TOPIC_FIELDS)
