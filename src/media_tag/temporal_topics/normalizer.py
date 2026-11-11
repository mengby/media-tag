import json
from media_tag.analysis.contracts import FieldAssessment, MEDICAL_FIELDS
from media_tag.contracts.models import FieldValue
from media_tag.local_topics.parser import _unique_object


def assess(raw,field,*,policy,versions,evidence_id):
    common=dict(field=field,origin='local',versions=versions,evidence_ids=(evidence_id,))
    def unresolved(state,reason,candidates=(),next_step='precision'):
        return FieldAssessment(**common,value=FieldValue(state=state,reason=reason),next_step=next_step,
            reason=reason,candidates=candidates)
    try:
        if len(raw)>16000:raise ValueError('answer_budget')
        data=json.loads(raw,object_pairs_hook=_unique_object)
        if not isinstance(data,dict) or set(data)!={'field','state','values','dynamic'} or data['field']!=field:
            raise ValueError('field_protocol')
        if data['state'] not in ('observed','unknown') or type(data['dynamic']) is not bool:
            raise ValueError('state_protocol')
        values=data['values']
        if not isinstance(values,list) or len(values)>8 or any(type(v) is not str or not v.strip() or len(v)>200 or '\x00' in v for v in values):
            raise ValueError('values_protocol')
        if data['state']=='unknown':
            if values or data['dynamic']:raise ValueError('unknown_payload')
            return unresolved('unknown','local_video_model_unknown')
        if not values or len(set(values))!=len(values):raise ValueError('values_protocol')
        audio_words=('bgm','旁白','音效','男声','女声','语速','听到','听起来','soundtrack','voiceover',
            'voice-over','narration','sound effect','background music','audible','speaking voice')
        if any(mark in value.casefold() for value in values for mark in audio_words):
            return unresolved('unknown','audio_not_analyzed',next_step='evidence')
        values=tuple(values);definition=policy.asset_fields[field]
        dynamic=data['dynamic'] or (field=='过程/状态' and any('→' in v or '->' in v for v in values))
        if definition.field_type in ('单选','多选') and (policy.options(definition) is None or not set(values)<=set(policy.options(definition))):
            item=unresolved('no_match','published_vocabulary_unmatched',values,'vocabulary')
            return item.model_copy(update={'dynamic':dynamic})
        if field in ('中文检索词','英文检索词') and definition.field_type not in ('多选','关联'):
            values=('；'.join(values),)
        value=FieldValue(state='observed',values=values);policy.validate(definition,value)
        return FieldAssessment(**common,value=value,next_step='human' if field in MEDICAL_FIELDS else 'precision',
            reason='continuous_visual_draft_requires_review',dynamic=dynamic)
    except (ValueError,TypeError,KeyError):
        return unresolved('failed','invalid_temporal_model_protocol')
