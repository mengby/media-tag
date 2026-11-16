import json

from media_tag.analysis.contracts import FieldAssessment, MEDICAL_FIELDS, TOPIC_FIELDS
from media_tag.contracts.models import FieldValue


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_json_key')
        result[key] = value
    return result


def assess_fields(names, answers, policy, versions, evidence_id):
    fields = {name: FieldAssessment(field=name, value=FieldValue(state='unknown', reason='not_requested'),
                                    next_step='local', reason='not_requested') for name in TOPIC_FIELDS}
    for name, answer in zip(names, answers, strict=True):
        fields[name] = assess(answer, name, policy=policy, versions=versions, evidence_id=evidence_id)
    return tuple(fields.values())


def parse_answer(raw, field, field_type, join_phrases):
    """Parse the wire protocol before vocabulary mapping and review decisions."""
    if len(raw) > 16000:
        raise ValueError('answer_budget')
    data = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(data, dict) or set(data) != {'field', 'state', 'values', 'dynamic'}:
        raise ValueError('answer_keys')
    if data['field'] != field or data['state'] not in ('observed', 'unknown') or type(data['dynamic']) is not bool:
        raise ValueError('answer_binding')
    values = data['values']
    if not isinstance(values, list) or len(values) > 8 or any(type(v) is not str or not v.strip() or len(v) > 200 or '\x00' in v for v in values):
        raise ValueError('answer_values')
    if data['state'] == 'unknown':
        if values or data['dynamic']:
            raise ValueError('unknown_answer_payload')
        return FieldValue(state='unknown', reason='local_model_unknown')
    if not values or len(set(values)) != len(values):
        raise ValueError('empty_or_duplicate_values')
    if data['dynamic'] or field == '过程/状态' or any('→' in v or '->' in v for v in values):
        return FieldValue(state='unknown', reason='static_evidence_cannot_establish_process')
    if field == '英文检索词' and any('\u3400' <= c <= '\u9fff' or '\uf900' <= c <= '\ufaff' for v in values for c in v):
        return FieldValue(state='unknown', reason='english_keywords_require_english_text')
    if join_phrases and field_type not in ('多选', '关联'):
        values = ['；'.join(values)]
    # Closed single-select ambiguity is resolved separately against the deployed vocabulary.
    if field_type not in ('单选', '多选', '关联') and len(values) != 1:
        raise ValueError('scalar field requires one value')
    return FieldValue(state='observed', values=tuple(values))


def protocol_fields(policy, fields):
    return [(name, policy.asset_fields[name].field_type,
             name in ('中文检索词', '英文检索词') or name in policy.tag_fields.guidance) for name in fields]


def assess(raw, field, *, policy, versions, evidence_id):
    common = dict(field=field, origin='local', versions=versions, evidence_ids=(evidence_id,))
    def unresolved(state, reason, *, candidates=(), next_step='precision'):
        return FieldAssessment(**common, value=FieldValue(state=state, reason=reason),
                               next_step=next_step, reason=reason, candidates=candidates)
    try:
        value = parse_answer(raw, *protocol_fields(policy, (field,))[0])
        if value.state == 'unknown':
            return unresolved('unknown', value.reason, next_step=(
                'evidence' if value.reason == 'static_evidence_cannot_establish_process' else 'precision'))
        definition = policy.asset_fields[field]
        values = value.values
        if definition.field_type in ('单选', '多选') and (policy.options(definition) is None or not set(values) <= set(policy.options(definition))):
            return unresolved('no_match', 'published_vocabulary_unmatched', candidates=values, next_step='vocabulary')
        if definition.field_type == '单选' and len(values) != 1:
            return unresolved('unknown', 'local_single_selection_ambiguous',
                next_step='human' if field in MEDICAL_FIELDS else 'precision')
        policy.validate(definition, value)
        return FieldAssessment(**common, value=value, next_step='human' if field in MEDICAL_FIELDS else 'precision',
                               reason='local_suggestion_requires_review' if field in MEDICAL_FIELDS else 'local_uncalibrated_draft')
    except (ValueError, TypeError, KeyError):
        return unresolved('failed', 'invalid_local_model_protocol')
