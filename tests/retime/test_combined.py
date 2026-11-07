import json
import pytest
from redaction.conftest import redaction,scanned
from media_tag.contracts.models import WorkerResult
from media_tag.files.service import FileService,FileError
from media_tag.router.runtime import LocalExecutor
from media_tag.transforms.models import RenderSubmission,RetimeSubmission,ReviewSubmission,RETIME_VERSION
from media_tag.transforms.retime_service import RetimeService
from media_tag.transforms.runtime import retime_stages,RedactionHandlers
from media_tag.variants.models import SourceSegment,PlanSpec,Recipe


@pytest.mark.parametrize('speed',[.5,1.25,2.])
def test_actual_redaction_retime_is_one_encode_and_maps_every_mask(scanned,speed,tools):
    env=scanned
    submitted=env.redactions.start_render(RenderSubmission(request_id='combined',revision_hash=env.revision['revision_hash'],mode='solid',speed=speed,
        segments=(SourceSegment(segment_id='cue',start_seconds=.2,end_seconds=1.2),)),principal=env.owner)
    assert env.executor.run_once()['accepted']
    run=env.redactions.get_run(submitted['run_id'],principal=env.owner)
    assert run['job']['state']=='succeeded' and run['privacy_review']=='pending'
    with env.database.transaction() as connection:
        row=connection.execute('SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?',(submitted['job_id'],)).fetchone()
    result=WorkerResult.model_validate_json(row[0]);ref=next(a for a in result.artifacts if a.kind=='redaction_coverage')
    report=json.loads((env.workspace/ref.ref).read_text())
    assert report['video_encode_passes']==1 and report['decoded_frame_count']==6 and report['speed']==speed
    assert report['segments'][0]['output_end_seconds']==pytest.approx(1.2/speed)
    assert all(f['regions'] and f['output_seconds']==pytest.approx(f['seconds']/speed) for f in report['frames'])
    asset=env.catalog.get_asset(run['asset_id'],principal=env.owner)
    with pytest.raises(FileError,match='privacy_review_pending'): FileService(env.router).open(asset.asset_id,asset.sha256,principal=env.owner)


def test_retiming_reviewed_masked_parent_requires_new_output_review(scanned,tools):
    env=scanned;env.redactions.reviewers=frozenset({env.owner.actor_id})
    original=env.redactions.start_render(RenderSubmission(request_id='masked',revision_hash=env.revision['revision_hash']),principal=env.owner)
    assert env.executor.run_once()['accepted']
    run=env.redactions.get_run(original['run_id'],principal=env.owner);parent=env.catalog.get_asset(run['asset_id'],principal=env.owner)
    plan=env.redactions.variants.get_plan(run['plan_id'],principal=env.owner)
    review=ReviewSubmission(expected_revision=1,output_sha256=parent.sha256,recipe_hash=plan['recipe_hash'],decision='approved',
        full_output_viewed=True,exclusions_checked=True,risk_intervals_checked=True,note='Synthetic test review only')
    env.redactions.review(parent.asset_id,review,principal=env.owner)
    env.router.stages.update(retime_stages(tools))
    env.router.register_worker(env.workers[0].actor_id,tuple(env.router.stages),principal=env.admin)
    service=RetimeService(env.router,tools,reviewers=(env.owner.actor_id,))
    advanced=service.variants.create_plan(PlanSpec(parent_asset_id=parent.asset_id,expected_revision=2,chain_from_derived=True,
        recipe=Recipe(speed=2.,renderer_revision=RETIME_VERSION,privacy_review_required=False)),request_id='cannot-clear-privacy',principal=env.owner)
    assert advanced['contract']['spec']['recipe']['privacy_review_required'] is True
    submitted=service.start(RetimeSubmission(request_id='retime-masked',parent_asset_id=parent.asset_id,expected_revision=2,speed=2.,chain_from_derived=True),principal=env.owner)
    executor=LocalExecutor(env.router,env.workers[0],RedactionHandlers(service),after_release=service.adopt_ready)
    assert executor.run_once()['accepted']
    done=service.get(submitted['job_id'],principal=env.owner);asset=env.catalog.get_asset(done['asset_id'],principal=env.owner)
    assert done['privacy_review']=='pending' and asset.sha256!=parent.sha256
    files=FileService(env.router)
    with pytest.raises(FileError,match='privacy_review_pending'): files.open(asset.asset_id,asset.sha256,principal=env.owner)
    private=service.review_files().open(asset.asset_id,asset.sha256,principal=env.owner);private.close()
    own=review.model_copy(update={'output_sha256':asset.sha256,'recipe_hash':done['recipe_hash']})
    service.review(asset.asset_id,own,principal=env.owner)
    opened=files.open(asset.asset_id,asset.sha256,principal=env.owner);opened.close()
    env.redactions.review(parent.asset_id,review.model_copy(update={'expected_revision':2,'decision':'revoked'}),principal=env.owner)
    with pytest.raises(FileError,match='privacy_review_revoked'): files.open(asset.asset_id,asset.sha256,principal=env.owner)
