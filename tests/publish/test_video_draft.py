import json

import pytest

from media_tag.publish.video_draft import VideoDraft


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
