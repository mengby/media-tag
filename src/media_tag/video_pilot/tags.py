"""Shared image fields and source-clock citations for the short-video tagger."""
import json
import math
from hashlib import sha256
from pathlib import Path

from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.analysis.fields import DEFAULT_FIELDS_FILE, TagFields
from media_tag.contracts.models import FieldValue
from media_tag.contracts.templates import load_template_bundle
from media_tag.precision.chat import compact_fields
from media_tag.precision.assessment import normalized_value
from media_tag.review.models import ReviewDeployment
from media_tag.review.policy import ReviewPolicy


VERSION = 'video-tags-v2'
BASE_FIELDS = (*TOPIC_FIELDS, '口语化表达')
ACTION = '画面动作'


def manifest_digest(manifest):
    data = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)
    return sha256(data.encode()).hexdigest()


def load_policy(template_dir=None):
    runtime = Path('data/local/runtime.local.json')
    settings = json.loads(runtime.read_bytes()) if template_dir is None and runtime.is_file() else {}
    contract = load_template_bundle(Path(template_dir or settings.get('template_dir', 'asset-index-template')),
                                    expected_fingerprint=settings.get('template_fingerprint'))
    review = {'tag_fields_file': str(DEFAULT_FIELDS_FILE), **settings.get('review', {})}
    return ReviewPolicy(contract, ReviewDeployment.model_validate_json(json.dumps(review)))


def field_definitions(policy):
    definitions = policy.asset_fields
    if '口语化表达' not in definitions:
        defaults = TagFields.load(DEFAULT_FIELDS_FILE, ())
        return defaults.definitions() | definitions, defaults.guidance | policy.tag_fields.guidance
    return definitions, policy.tag_fields.guidance


def prompt_fields(policy):
    definitions, guidance = field_definitions(policy)
    fields = {}
    for name in BASE_FIELDS:
        definition = definitions[name]
        spec = {'类型': definition.field_type}
        if definition.field_type in ('单选', '多选'):
            spec['已发布选项'] = policy.options(definition) or []
        if name in guidance:
            spec['填写说明'] = guidance[name]
        fields[name] = spec
    return fields


def normalize_fields(values, policy, actions):
    # Decode nulls and lists exactly as in the compact image adapter.
    decoded = compact_fields({'request_id': '', 'values': values}, BASE_FIELDS)
    tags, candidates = {}, {}
    definitions, _ = field_definitions(policy)
    for item in decoded['fields']:
        name = item['field']
        value = FieldValue.model_validate_json(json.dumps(item['value']))
        tags[name] = None
        definition = definitions[name]
        value, unmatched = normalized_value(value, definition, policy)
        if unmatched:
            candidates[name] = list(unmatched)
        if value.state == 'observed':
            if name == '过程/状态' and not actions and any('→' in v or '->' in v for v in value.values):
                raise ValueError('directional process requires action evidence')
            tags[name] = list(value.values) if definition.field_type == '多选' else value.values[0]
    return tags, candidates


def bind_actions(actions, entries, *, stored=False):
    """Resolve each cited pair of frame indices to its own observed source interval."""
    if not isinstance(actions, list) or len(actions) > 16:
        raise ValueError('invalid video actions')
    by_id = {entry['id']: entry for entry in entries}
    if len(by_id) != len(entries):
        raise ValueError('duplicate evidence ID')
    bound = []
    for action in actions:
        if not isinstance(action, dict) or set(action) != {'text', 'evidence'}:
            raise ValueError('invalid video action')
        phrase, citations = action['text'], action['evidence']
        if (not isinstance(phrase, str) or not phrase.strip() or len(phrase) > 2000
                or not isinstance(citations, list) or not 1 <= len(citations) <= 16):
            raise ValueError('invalid action text or evidence')
        evidence = []
        for citation in citations:
            keys = {'id', 'first_frame', 'last_frame'}
            if stored:
                keys.update(('start_seconds', 'end_seconds', 'sha256'))
            if not isinstance(citation, dict) or set(citation) != keys or not isinstance(citation['id'], str):
                raise ValueError('invalid action citation')
            entry = by_id.get(citation['id'])
            if not entry or entry['kind'] != 'sequence':
                raise ValueError('action requires a continuous evidence window')
            first, last = citation['first_frame'], citation['last_frame']
            times = entry['frame_seconds']
            if type(first) is not int or type(last) is not int or not 1 <= first < last <= len(times):
                raise ValueError('invalid cited frame range')
            if not all(type(t) in (int, float) and math.isfinite(t) and entry['start'] <= t <= entry['end']
                       for t in times) or any(a >= b for a, b in zip(times, times[1:])):
                raise ValueError('invalid source clock')
            segment = dict(id=entry['id'], first_frame=first, last_frame=last, start_seconds=times[first-1],
                           end_seconds=times[last-1], sha256=entry['sha256'])
            if stored and segment != citation:
                raise ValueError('action range or evidence hash differs from manifest')
            if segment not in evidence:
                evidence.append(segment)
        bound.append({'text': phrase.strip(), 'evidence': evidence})
    return bound
