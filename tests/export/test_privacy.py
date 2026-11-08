from pathlib import Path
import pytest
from media_tag.catalog import RevisionConflict
from media_tag.export.models import ExportSubmission,AssetSelection
from media_tag.models.registry import ModelRegistry
from media_tag.router.runtime import LocalExecutor
from media_tag.transforms.service import RedactionService
from media_tag.transforms.runtime import redaction_stages,RedactionHandlers
from media_tag.transforms.models import ScanSubmission,RevisionSubmission,RenderSubmission,ReviewSubmission
from review.test_intervals import video
from .test_export import rows


def test_csv_respects_output_privacy_and_revocation_without_approving_medical_tags(reviewed):
    env=reviewed;video(env)
    registry=ModelRegistry(Path('cache/models/d04').resolve())
    env.router.stages.update(redaction_stages(registry,env.tools))
    env.router.register_worker(env.worker.actor_id,tuple(env.router.stages),principal=env.admin)
    redactions=RedactionService(env.router,env.tools,registry,reviewers=(env.owner.actor_id,))
    worker=LocalExecutor(env.router,env.worker,RedactionHandlers(redactions),after_release=redactions.adopt_ready)
    job=redactions.start_scan(ScanSubmission(request_id='synthetic-export-scan',parent_asset_id=env.asset.asset_id,expected_revision=2),principal=env.owner)
    assert worker.run_once()['accepted']
    scan=redactions.get_scan(job['job_id'],principal=env.owner)
    revision=redactions.create_revision(RevisionSubmission(**{key:scan[key] for key in ('scan_task_id','lease_epoch','report_sha256')}),principal=env.owner)
    run=redactions.start_render(RenderSubmission(request_id='synthetic-export-render',revision_hash=revision['revision_hash']),principal=env.owner)
    assert worker.run_once()['accepted']
    result=redactions.get_run(run['run_id'],principal=env.owner)
    asset=env.catalog.get_asset(result['asset_id'],principal=env.owner)
    spec=ExportSubmission(request_id='pending',assets=(AssetSelection(asset_id=asset.asset_id,expected_revision=1,source_sha256=asset.sha256),))
    with pytest.raises(RevisionConflict):env.exports.create(spec,principal=env.owner)
    details=env.reviews.files.details(asset.asset_id,principal=env.owner)
    review=ReviewSubmission(expected_revision=1,output_sha256=asset.sha256,recipe_hash=details['transform']['recipe_hash'],decision='approved',
        full_output_viewed=True,exclusions_checked=True,risk_intervals_checked=True,note='Synthetic protocol test on abstract generated video; not a production privacy review')
    redactions.review(asset.asset_id,review,principal=env.owner)
    selected=AssetSelection(asset_id=asset.asset_id,expected_revision=2,source_sha256=asset.sha256)
    exported=env.exports.create(ExportSubmission(request_id='approved',assets=(selected,)),principal=env.owner)
    item=dict(zip(*rows(env.exports.content(exported['export_id'],'assets.csv',principal=env.owner))))
    assert item['含人脸']=='已处理副本' and item['标签状态']=='AI初稿'
    redactions.review(asset.asset_id,review.model_copy(update={'expected_revision':2,'decision':'revoked'}),principal=env.owner)
    assert env.clients.owner.get('/review/exports/'+exported['export_id']+'/content/assets.csv').status_code==409
