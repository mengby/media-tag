"""Reuse current synthetic CLIP reports through the real evidence verifier, no model call."""
import pytest

from search.conftest import sought, env
from search.test_ai_drafts import modeled

from media_tag.catalog import RevisionConflict
from media_tag.router.models import StepSpec
from media_tag.router.models import JobSpec
from media_tag.router.runtime import LocalExecutor
from media_tag.contracts.models import Versions
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.similarity.contracts import ClipFingerprint, SourceVersion
from media_tag.similarity.engines import SimilarityError, compare
from media_tag.similarity.service import AuthorizedSimilarity
from media_tag.similarity.ledger import RelationDecision, RelationLedger


def descriptor(model,tools):
    asset=model.env.catalog.get_asset(model.sought.asset.asset_id,principal=model.env.owner)
    source=SourceVersion(asset_id=asset.asset_id,asset_revision=asset.revision,content_sha256=asset.sha256)
    service=AuthorizedSimilarity(model.env.router,tools)
    return service,service.describe_clip(source,principal=model.env.owner)


def test_existing_clip_features_are_reused_without_requesting_inference_or_publishing_vocab(modeled,tools):
    m=modeled
    with m.env.database.transaction() as connection:
        jobs=connection.execute('SELECT COUNT(*) FROM router_jobs').fetchone()[0]
    service,result=descriptor(m,tools)
    assert result.model_revision==m.revision and result.producer_task_id and len(result.vectors[0])==512
    assert result.coverage=='verified_current_clip_sample_features' and not result.full_video_observed
    service.validate_descriptor(result,principal=m.env.owner)
    with m.env.database.transaction() as connection:
        assert connection.execute('SELECT COUNT(*) FROM router_jobs').fetchone()[0]==jobs


@pytest.mark.parametrize('change',['cancel','tamper','new_pending'])
def test_clip_descriptors_become_unusable_when_their_current_producer_changes(modeled,tools,change):
    m=modeled;service,result=descriptor(m,tools)
    if change=='cancel':
        m.env.router.cancel(m.job['job_id'],principal=m.env.owner)
    elif change=='tamper':
        m.reports['clip'].write_text('{}')
    else:
        with m.env.database.transaction() as connection:
            row=connection.execute("SELECT spec_json FROM router_tasks WHERE job_id=? AND active=1 AND step_key='clip'",(m.job['job_id'],)).fetchone()
        job=m.env.router.get_job(m.job['job_id'],principal=m.env.owner)
        m.env.router.replan(job['job_id'],(StepSpec.model_validate_json(row[0]),),expected_revision=job['revision'],principal=m.env.owner)
    with pytest.raises(RevisionConflict):
        service.candidates(result,[],principal=m.env.owner)


def test_clip_vectors_from_different_models_are_not_compared_as_one_profile(modeled,tools):
    _,left=descriptor(modeled,tools)
    raw=left.model_dump(mode='json')
    raw['source']['content_sha256']='f'*64
    raw['model_revision']='different-model'
    import json
    other=ClipFingerprint.model_validate_json(json.dumps(raw))
    with pytest.raises(SimilarityError,match='incompatible_descriptor_profiles'):
        compare(left,other)


def test_invalid_clip_numeric_norm_cannot_turn_into_a_perfect_candidate(modeled,tools):
    _,value=descriptor(modeled,tools)
    raw=value.model_dump(mode='json');raw['vectors']=[[1e308]*512]
    import json
    with pytest.raises(ValueError):
        ClipFingerprint.model_validate_json(json.dumps(raw))


def test_relation_cannot_reuse_old_clip_decision_after_producer_cancelled(modeled,tools,tmp_path):
    m=modeled;service,left=descriptor(m,tools)
    originals=m.sought.original.parent
    clone=originals/'synthetic-clone.png';clone.write_bytes(m.sought.original.read_bytes())
    asset=m.env.catalog.register_file(FilesystemBackend(originals),clone.name,source_backend_id='synthetic-clip-clone',
        principal=m.env.owner,request_id='clip-clone',name='Synthetic clone')
    versions=Versions(rules_version='synthetic',model_revision=m.revision,preprocess_version='synthetic-frames')
    job=m.env.router.submit(JobSpec(asset_id=asset.asset_id,steps=(StepSpec(key='sample',stage='sample',versions=versions),
        StepSpec(key='clip',stage='clip',depends_on=('sample',),versions=versions))),request_id='clone-job',principal=m.env.owner)
    executor=LocalExecutor(m.env.router,m.worker,m.handler)
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    right=service.describe_clip(SourceVersion(asset_id=asset.asset_id,asset_revision=asset.revision,content_sha256=asset.sha256),principal=m.env.owner)
    ledger=RelationLedger(tmp_path/'clip-relations',service)
    offered=ledger.offer(compare(left,right),request_id='clip-offer',principal=m.env.owner)
    spec=RelationDecision(request_id='clip-decision',expected_revision=1,relation='same_recording',note='Synthetic identical geometry bytes')
    assert ledger.decide(offered['relation_id'],spec,principal=m.env.owner)['revision']==2
    m.env.router.cancel(job['job_id'],principal=m.env.owner)
    with pytest.raises(RevisionConflict):
        ledger.decide(offered['relation_id'],spec,principal=m.env.owner)
    assert ledger.list(principal=m.env.owner)==[]
