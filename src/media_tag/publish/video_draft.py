"""Bind verified local Qwen video samples and draft labels to a single WY publication."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from media_contracts import Blob, Evidence, EvidencePayload, Field, canonical
from media_tag.cleaning.pretag import current_screen
from media_tag.video_pilot.api import validate_tags
from .items import ready_version


class VideoDraft:
    def __init__(self, folder, source_sha256, model_revision):
        self.folder = Path(folder).resolve(strict=True)
        self.source = source_sha256
        self.model = model_revision
        self.raw = (self.folder / 'tags.json').read_bytes()
        wire = (self.folder / 'wire.json').read_bytes()
        self.manifest = json.loads(wire)
        result = json.loads(self.raw)
        if result['source_sha256'] != self.source or self.manifest['source_sha256'] != self.source:
            raise ValueError('video draft source mismatch')
        entries = self.manifest['evidence']
        if not entries or len({e['id'] for e in entries}) != len(entries):
            raise ValueError('missing or duplicate video evidence')
        self.tags = validate_tags(result['tags'], entries)
        self.fingerprint = sha256(canonical([source_sha256, model_revision,
            sha256(self.raw).hexdigest(), sha256(wire).hexdigest()])).hexdigest()

    def export(self, files, conn, asset, detail, principal, item, save, save_json):
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
        tags = self.tags
        fields = [('画面主体', tags['subjects']), ('表现形式', [tags['form']]), ('中文检索词', tags['keywords']),
                  ('画面动作', [a['text'] for a in tags['actions']])]
        payload = EvidencePayload(asset_id=asset_id, source_sha256=self.source, task_id=report_id, stage='visual_fields',
            producer_report_sha256=report.sha256, producer_model_revision=self.model,
            labels=tuple(dict.fromkeys(v for _, values in fields for v in values)), frames=tuple(frames))
        evidence = Evidence(evidence_id=report_id, blob=save_json(payload), source_sha256=self.source)
        published = [Field(field=name, values=tuple(values), origin='ai_draft', evidence_ids=(report_id,))
                     for name, values in fields if values]
        action_ids = list(dict.fromkeys(i for action in tags['actions'] for i in action['evidence']))
        metadata = dict(asset_id=asset_id, source_sha256=self.source, tag_version=item['tag_version'],
            previous_release=item['previous_release'], watermark_state=screen['report']['watermark']['state'],
            evidence=evidence_map, fields=[dict(field=name, state='observed' if values else 'unknown', values=values,
                candidates=[], evidence_ids=([report_id]+(action_ids if name=='画面动作' else frame_ids)) if values else [],
                requires_field_review=False) for name, values in fields])
        return metadata, published, evidence
