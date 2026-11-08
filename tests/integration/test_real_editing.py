"""Boundary tests for the real-task harness, using synthetic local files only."""
import importlib.util
import json
from pathlib import Path
import sys
import pytest

spec=importlib.util.spec_from_file_location('real_editing_harness',Path(__file__).with_name('real_editing.py'))
harness=importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


def test_copy_follows_file_references_not_arbitrary_strings_or_prior_approvals(tmp_path):
    source=tmp_path/'source';source.mkdir();target=tmp_path/'target';target.mkdir()
    for name in ('cut.preview.mp4','cut.preview.report.json','cut.transcript.json','audio.processing.json',
                 'style.resolved.json','editorial-execution.json'):
        (source/name).write_text('{}',encoding='utf8')
    evidence=source/'evidence';evidence.mkdir()
    (source/'preparation.json').write_text(json.dumps({'evidence_checks':'evidence/checks.json'}),encoding='utf8')
    (evidence/'checks.json').write_text(json.dumps({'records':[
        {'path':'evidence\\text.txt','irrelevant':'evidence/unreferenced.txt'},
        {'source_file':'evidence/text.txt'},{'text_file':'../outside.txt'},
        {'path':'evidence/approved-old.json'},{'path':'evidence/key.local.json'},
        {'path':'evidence/.env'},{'path':'evidence/acceptance.json'}]}),encoding='utf8')
    for name in ('text.txt','unreferenced.txt','approved-old.json','key.local.json','.env','acceptance.json'):
        (evidence/name).write_text(name,encoding='utf8')
    before=harness.snapshot(source)
    copied=harness.copy_inputs(source,target)
    assert copied.count('evidence/text.txt')==1
    assert (target/'evidence/text.txt').read_bytes()==(evidence/'text.txt').read_bytes()
    assert set(p.name for p in (target/'evidence').iterdir())=={'checks.json','text.txt','README.md'}
    assert len(copied)==9
    assert harness.snapshot(source)==before


def test_snapshot_records_bytes_and_never_reads_environment_file(tmp_path):
    (tmp_path/'input.txt').write_text('source',encoding='utf8')
    (tmp_path/'.env').write_text('synthetic-private-placeholder',encoding='utf8')
    before=harness.snapshot(tmp_path)
    assert before=={'input.txt':harness.digest(tmp_path/'input.txt')}
    assert (tmp_path/'input.txt').read_text(encoding='utf8')=='source'


def test_image_submission_requires_explicit_permission_before_host_call(monkeypatch,tmp_path):
    monkeypatch.setattr(sys,'argv',['real_editing.py','review','--batch',str(tmp_path)])
    monkeypatch.setattr(harness,'review',lambda batch:pytest.fail('host called without permission'))
    with pytest.raises(ValueError,match='explicit permission'):
        harness.main()


def test_invalid_task_id_is_rejected_before_reading_host():
    with pytest.raises(ValueError,match='invalid source task'):
        harness.prepare('../outside')
