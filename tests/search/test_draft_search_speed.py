"""Candidate filtering must preserve full-verification semantics and immediate invalidation."""
from contextlib import contextmanager
import json
import os

import pytest
from fastapi.testclient import TestClient

from media_tag.contracts.models import Versions
from media_tag.router.models import JobSpec,StepSpec
from .test_ai_drafts import modeled
from .test_visual_fields import visual
from .test_query import query


def stable(result):
    return {k:result[k] for k in ('items','index_revision','empty_reason','intent','selected_concept_ids','next')}


@pytest.mark.parametrize('text',['唯一画中文字','检测到人脸','不存在的模型文字','合成圆形','没有人脸'])
def test_index_filter_results_equal_full_verification(modeled,monkeypatch,text):
    m=modeled;m.sync()
    actual=stable(query(m.sought,text,include_drafts=True))
    original=m.sought.search.drafts.sync
    def full(c,asset,**kwargs):return original(c,asset,persist=False)
    monkeypatch.setattr(m.sought.search.drafts,'sync',full)
    assert actual==stable(query(m.sought,text,include_drafts=True))


def test_only_matching_current_stage_reads_bytes_but_details_still_verify_all(modeled,monkeypatch):
    m=modeled;m.sync();opened=[];original=m.env.router.files.open_read
    @contextmanager
    def track(key):
        opened.append(key)
        with original(key) as stream:yield stream
    monkeypatch.setattr(m.env.router.files,'open_read',track)
    assert query(m.sought,'唯一画中文字',include_drafts=True)['items']
    assert m.reports['ocr'].relative_to(m.env.workspace).as_posix() in opened
    assert all(m.reports[s].relative_to(m.env.workspace).as_posix() not in opened for s in ('face','clip'))
    opened.clear();assert not query(m.sought,'完全不在索引中的词',include_drafts=True)['items']
    model_paths={p.relative_to(m.env.workspace).as_posix() for p in m.reports.values()}
    model_paths.add(json.loads(m.reports['ocr'].read_text())['frames'][0]['artifact']['ref'])
    assert not model_paths.intersection(opened)  # Published-vocabulary checks still run.
    m.sync()
    assert all(m.reports[s].relative_to(m.env.workspace).as_posix() in opened for s in ('ocr','face','clip'))


@pytest.mark.parametrize('target',['report','frame'])
def test_warm_candidate_detects_same_size_tamper_even_with_restored_mtime(modeled,target):
    m=modeled;m.sync();assert query(m.sought,'唯一画中文字',include_drafts=True)['items']
    path=m.reports['ocr']
    if target=='frame':path=m.env.workspace/json.loads(path.read_text())['frames'][0]['artifact']['ref']
    stamp=path.stat();raw=path.read_bytes();path.write_bytes(bytes([raw[0]^1])+raw[1:])
    os.utime(path,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
    assert path.stat().st_size==stamp.st_size and path.stat().st_mtime_ns==stamp.st_mtime_ns
    assert not query(m.sought,'唯一画中文字',include_drafts=True)['items']
    response=TestClient(m.sought.app).post('/v1/assets/search',json={'query':'唯一画中文字','include_drafts':True,'use_model':False},headers={'Authorization':'Bearer reader'})
    assert response.status_code==200 and not response.json()['items']


def test_missing_or_obsolete_projection_falls_back_to_real_evidence(modeled):
    m=modeled
    with m.env.database.transaction(write=True) as c:c.execute('DELETE FROM ai_draft_documents')
    assert query(m.sought,'唯一画中文字',include_drafts=True)['items']
    m.sync()
    with m.env.database.transaction(write=True) as c:
        c.execute("UPDATE ai_draft_documents SET basis_sha256=? WHERE stage='ocr'",('0'*64,))
    assert query(m.sought,'唯一画中文字',include_drafts=True)['items']


def test_warm_search_rechecks_permissions_membership_and_task_state(modeled):
    m=modeled;m.sync()
    assert query(m.sought,'唯一画中文字',include_drafts=True,actor=m.env.reader)['items']
    with m.env.database.transaction(write=True) as c:
        c.execute('DELETE FROM asset_permissions WHERE asset_id=? AND subject_id=?',(m.sought.asset.asset_id,m.env.reader.actor_id))
    assert not query(m.sought,'唯一画中文字',include_drafts=True,actor=m.env.reader)['items']
    assert query(m.sought,'唯一画中文字',include_drafts=True)['items']
    with m.env.database.transaction(write=True) as c:c.execute('UPDATE library_entries SET active=0 WHERE asset_id=?',(m.sought.asset.asset_id,))
    assert not query(m.sought,'唯一画中文字',include_drafts=True)['items']
    assert query(m.sought,'唯一画中文字',include_drafts=True,workspace=True)['items']
    with m.env.database.transaction(write=True) as c:c.execute('UPDATE router_jobs SET cancelled=1 WHERE job_id=?',(m.job['job_id'],))
    assert not query(m.sought,'唯一画中文字',include_drafts=True,workspace=True)['items']


def test_pending_new_version_never_reuses_old_matching_stage(modeled):
    m=modeled;m.sync();assert query(m.sought,'唯一画中文字',include_drafts=True)['items']
    step=StepSpec(key='ocr',stage='ocr',versions=Versions(rules_version='new',model_revision=m.revision,preprocess_version='new-frames'))
    m.env.router.submit(JobSpec(asset_id=m.sought.asset.asset_id,steps=(step,)),request_id='speed-new-version',principal=m.env.owner)
    assert not query(m.sought,'唯一画中文字',include_drafts=True)['items']


def test_read_view_cannot_cross_connections_or_write_projections(modeled):
    m=modeled;m.sync();index=m.sought.search.drafts
    with m.env.database.transaction() as c:
        asset=m.env.catalog._authorize(c,m.sought.asset.asset_id,m.env.owner)
        view=index.read_view(c,[asset])
        with pytest.raises(ValueError):index.sync(c,asset,read_view=view)
        with pytest.raises(ValueError):index.sync(c,asset,select_document=lambda doc:False)
        with m.env.database.transaction() as other:
            with pytest.raises(ValueError):index.sync(other,asset,persist=False,read_view=view)


def test_visual_fields_and_normalized_substrings_keep_same_evidence_and_order(visual,monkeypatch):
    v=visual;v.add();assert v.executor.run_once()['accepted'];v.m.sync()
    for word in ('红色圆形','RED CIRCLE','抽象几何','2D插图'):
        actual=stable(query(v.m.sought,word,include_drafts=True))
        assert actual['items']
        original=v.m.sought.search.drafts.sync
        with monkeypatch.context() as patch:
            patch.setattr(v.m.sought.search.drafts,'sync',lambda c,a,**kw:original(c,a,persist=False))
            assert actual==stable(query(v.m.sought,word,include_drafts=True))


def test_visual_candidates_still_verify_all_relevant_embeddings(modeled,monkeypatch):
    from types import SimpleNamespace
    m=modeled;m.sync()
    class Encoder:
        registry=SimpleNamespace(revision=m.revision)
        def encode(self,text):return [1.]+[0.]*511
    m.sought.search.visual_encoder=Encoder()
    actual=stable(query(m.sought,'额外视觉形状',include_drafts=True,include_visual_drafts=True))
    assert actual['items'];original=m.sought.search.drafts.sync
    with monkeypatch.context() as patch:
        patch.setattr(m.sought.search.drafts,'sync',lambda c,a,**kw:original(c,a,persist=False))
        assert actual==stable(query(m.sought,'额外视觉形状',include_drafts=True,include_visual_drafts=True))
    m.reports['clip'].write_bytes(b'changed embedding report')
    assert not query(m.sought,'额外视觉形状',include_drafts=True,include_visual_drafts=True)['items']
