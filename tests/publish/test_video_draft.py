import json
from hashlib import sha256
from types import SimpleNamespace

import pytest

from media_contracts import Blob, canonical
from media_tag.publish.video_draft import VideoDraft
from media_tag.video_pilot.tags import ACTION, BASE_FIELDS, VERSION, manifest_digest


def write_reports(folder, *, cited="C1", wire_source=None, keyword="运动"):
    source = "a" * 64
    tags = {"subjects": ["人"], "form": "实拍", "keywords": [keyword],
            "actions": [{"text": "人转身", "evidence": [cited]}]}
    (folder / "tags.json").write_text(json.dumps({"source_sha256": source, "tags": tags}))
    wire = {"source_sha256": wire_source or source, "evidence": [
        {"id": "C1", "kind": "sequence", "frame_seconds": [0.1, 0.2]}]}
    (folder / "wire.json").write_text(json.dumps(wire))
    return source


def test_video_version_binds_report_and_model(tmp_path):
    source = write_reports(tmp_path)
    first = VideoDraft(tmp_path, source, "model-a")
    assert first.fingerprint == VideoDraft(tmp_path, source, "model-a").fingerprint
    assert first.fingerprint != VideoDraft(tmp_path, source, "model-b").fingerprint
    write_reports(tmp_path, keyword="转身")
    assert first.fingerprint != VideoDraft(tmp_path, source, "model-a").fingerprint


def test_rejects_frames_from_another_source(tmp_path):
    source = write_reports(tmp_path, wire_source="b" * 64)
    with pytest.raises(ValueError, match="source mismatch"):
        VideoDraft(tmp_path, source, "model-a")


def test_rejects_action_citing_an_unknown_clip(tmp_path):
    source = write_reports(tmp_path, cited="C3")
    with pytest.raises(ValueError, match="invalid action evidence"):
        VideoDraft(tmp_path, source, "model-a")


def test_export_all_fields_preserves_review_and_per_action_evidence(tmp_path, sought, monkeypatch):
    source = 'a'*64
    image = b'synthetic sequence bytes'
    digest = sha256(image).hexdigest()
    (tmp_path / 'sequence.jpg').write_bytes(image)
    entry = dict(id='C1', kind='sequence', start=2, end=3, frame_seconds=[2.1, 2.5],
                 sha256=digest, path='sequence.jpg', bytes=len(image))
    manifest = dict(source_sha256=source, evidence=[entry])
    (tmp_path / 'samples.json').write_bytes(canonical(manifest))
    citation = dict(id='C1', first_frame=1, last_frame=2, start_seconds=2.1, end_seconds=2.5, sha256=digest)
    tags = dict.fromkeys(BASE_FIELDS) | {'科室': ['合成科室-仅测试'], '口语化表达': '圆形在移动',
        ACTION: [dict(text='圆形移动', evidence=[citation])]}
    raw = canonical(dict(schema_version=VERSION, source_sha256=source, evidence_manifest_sha256=manifest_digest(manifest),
                         tags=tags, candidates={'疾病/症状': ['未发布词']}))
    (tmp_path / 'tags.json').write_bytes(raw)
    draft = VideoDraft(tmp_path, source, 'model-test')
    blobs = {}
    def save(read, expected=None):
        data = read()
        blob = Blob(sha256=sha256(data).hexdigest(), size_bytes=len(data))
        assert expected is None or blob == expected
        blobs[blob.sha256] = data
        return blob

    monkeypatch.setattr('media_tag.publish.video_draft.ready_version', lambda *a, **kw: ('asset', 'v2'))
    monkeypatch.setattr('media_tag.publish.video_draft.current_screen', lambda *a: {
        'job_id': 'job', 'state': 'succeeded', 'report': {'watermark': {'state': 'unknown'}}})
    metadata, fields, evidence = draft.export(None, None, {'asset_id': 'asset', 'blob_sha256': source},
        {'technical': {'kind': 'video'}}, SimpleNamespace(actor_id='tester'),
        dict(job_id='job', tag_version='v2', previous_release=None), save,
        lambda value: save(lambda: canonical(value)), policy=sought.reviews.policy)
    by_name = {f['field']: f for f in metadata['fields']}
    assert set(by_name) == {*BASE_FIELDS, ACTION}
    assert by_name['科室']['requires_field_review'] and by_name['科室']['values'] == ['合成科室-仅测试']
    assert by_name['疾病/症状']['state'] == 'no_match' and by_name['疾病/症状']['candidates'] == ['未发布词']
    assert by_name['情绪/语气']['state'] == 'unknown'
    assert {f.field for f in fields} == {'口语化表达', ACTION}
    assert by_name[ACTION]['evidence_ids'] == [evidence.evidence_id, 'C1']
    published = json.loads(blobs[metadata['evidence'][evidence.evidence_id]])
    segment = published['tags'][ACTION][0]['evidence'][0]
    assert segment == citation and blobs[segment['sha256']] == image
    entry['end'] = 5
    (tmp_path / 'samples.json').write_bytes(canonical(manifest))
    with pytest.raises(ValueError, match='manifest changed'):
        VideoDraft(tmp_path, source, 'model-test')
