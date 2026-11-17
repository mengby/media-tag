"""Bind verified local Qwen video samples and draft labels to a single WY publication."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from media_contracts import Blob, Evidence, EvidencePayload, Field, canonical
from media_tag.analysis.contracts import MEDICAL_FIELDS
from media_tag.cleaning.pretag import current_screen
from media_tag.contracts.vocabulary import parse_json
from media_tag.video_pilot.api import validate_tags as validate_legacy_tags
from media_tag.video_pilot.tags import ACTION, BASE_FIELDS, VERSION, bind_actions, load_policy, manifest_digest, normalize_fields
from .items import ready_version


class VideoDraft:
    def __init__(self, folder, source_sha256, model_revision):
        self.folder = Path(folder).resolve(strict=True)
        self.source = source_sha256
        self.model = model_revision
        self.raw = (self.folder / 'tags.json').read_bytes()
        manifest = self.folder / 'wire.json'
        if not manifest.exists():
            manifest = self.folder / 'samples.json'
        wire = manifest.read_bytes()
        self.manifest = parse_json(wire)
        result = parse_json(self.raw)
        if result['source_sha256'] != self.source or self.manifest['source_sha256'] != self.source:
            raise ValueError('video draft source mismatch')
        entries = self.manifest['evidence']
        if not entries or len({e['id'] for e in entries}) != len(entries):
            raise ValueError('missing or duplicate video evidence')
        self.candidates = {}
        if 'schema_version' not in result:
            legacy = self.tags = validate_legacy_tags(result['tags'], entries)
            self.values = {'画面主体': legacy['subjects'], '表现形式': [legacy['form']], '中文检索词': legacy['keywords']}
            self.actions = legacy['actions']
            self.action_ids = [ident for action in self.actions for ident in action['evidence']]
            self.version = None
        elif result['schema_version'] == VERSION:
            if result.get('evidence_manifest_sha256') != manifest_digest(self.manifest):
                raise ValueError('video evidence manifest changed')
            self.version = VERSION
            self.tags = result['tags']
            if not isinstance(self.tags, dict) or set(self.tags) != {*BASE_FIELDS, ACTION}:
                raise ValueError('incomplete video fields')
            self.values = self.tags.copy()
            self.actions = bind_actions(self.values.pop(ACTION), entries, stored=True)
            self.action_ids = [cite['id'] for action in self.actions for cite in action['evidence']]
            self.candidates = result['candidates']
            if not isinstance(self.candidates, dict) or any(
                name not in BASE_FIELDS or self.values[name] is not None or not isinstance(terms, list)
                or not terms or not all(isinstance(v, str) and v.strip() for v in terms)
                for name, terms in self.candidates.items()
            ):
                raise ValueError('invalid video field candidates')
        else:
            raise ValueError('unknown video tag schema')
        self.fingerprint = sha256(canonical([source_sha256, model_revision,
            sha256(self.raw).hexdigest(), sha256(wire).hexdigest()])).hexdigest()

    def export(self, files, conn, asset, detail, principal, item, save, save_json, *, policy=None):
        asset_id = asset['asset_id']
        version = ready_version(conn, item['job_id'], principal.actor_id, video_fingerprint=self.fingerprint)
        if (version != (asset_id, item['tag_version']) or asset['blob_sha256'] != self.source
                or detail['technical'].get('kind') != 'video'):
            raise ValueError('video version or registered source changed')
        screen = current_screen(files, asset_id, principal)
        if screen.get('job_id') != item['job_id'] or screen['state'] != 'succeeded':
            raise ValueError('video cleaning report unavailable')
        frames = []
        evidence_map = {}
        for e in self.manifest['evidence']:
            path = (self.folder / e['path']).resolve(strict=True)
            if not path.is_relative_to(self.folder) or e['kind'] not in ('image', 'sequence'):
                raise ValueError('invalid video evidence path or kind')
            with path.open('rb') as stream:
                blob = save(stream.read, Blob(sha256=e['sha256'], size_bytes=e['bytes']))
            frames.append(blob)
            evidence_map[e['id']] = blob.sha256
        report = save(BytesIO(self.raw).read)
        report_id = 'video-' + self.fingerprint[:40]
        frame_ids = list(evidence_map)
        evidence_map[report_id] = report.sha256
        candidates = dict(self.candidates)
        if self.version == VERSION:
            normalized, unmatched = normalize_fields(self.values, policy or load_policy(), self.actions)
            candidates.update(unmatched)
            fields = {name: ([value] if isinstance(value, str) else value or []) for name, value in normalized.items()}
        else:
            fields = dict(self.values)
        fields[ACTION] = [action['text'] for action in self.actions]
        payload = EvidencePayload(asset_id=asset_id, source_sha256=self.source, task_id=report_id, stage='visual_fields',
            producer_report_sha256=report.sha256, producer_model_revision=self.model,
            labels=tuple(dict.fromkeys(v for name, values in fields.items() if name not in MEDICAL_FIELDS
                                       for v in values)), frames=tuple(frames))
        evidence = Evidence(evidence_id=report_id, blob=save_json(payload), source_sha256=self.source)
        published = [Field(field=name, values=tuple(values), origin='ai_draft', evidence_ids=(report_id,))
                     for name, values in fields.items() if values and name not in MEDICAL_FIELDS]
        action_ids = list(dict.fromkeys(self.action_ids))
        metadata = dict(asset_id=asset_id, source_sha256=self.source, tag_version=item['tag_version'],
            previous_release=item['previous_release'], watermark_state=screen['report']['watermark']['state'],
            evidence=evidence_map, fields=[dict(field=name,
                state='no_match' if name in candidates else 'observed' if values else 'unknown', values=values,
                candidates=candidates.get(name, []),
                evidence_ids=([report_id]+(action_ids if name == ACTION else frame_ids)) if values or name in candidates else [],
                requires_field_review=name in MEDICAL_FIELDS) for name, values in fields.items()])
        return metadata, published, evidence
