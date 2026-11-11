from types import SimpleNamespace

import pytest

from media_tag.cleaning import CleaningRecipe
from media_tag.export.models import AssetSelection,ExportSubmission
from media_tag.image_processing import ImageRecipe,MaskRegion
from media_tag.processing_queue import CleaningSubmission,ImageSubmission,ProcessingDeployment,ProcessingHandlers,ProcessingService,stages
from media_tag.processing_queue.derived import ProcessingDerivedService
from media_tag.processing_queue.derived_contracts import Reviewers
from media_tag.router.runtime import LocalExecutor

from review.conftest import decide,observation
from processing_queue.test_derived import approve
from .test_export import rows


@pytest.mark.parametrize('masked',[False,True])
def test_privacy_approval_alone_does_not_allow_processed_face_label_or_export_marker(reviewed,masked):
    e=reviewed;actor=e.owner.actor_id
    options=ProcessingDeployment(enabled=True,reviewers=Reviewers(quality=(actor,),privacy=(actor,)))
    e.router.stages.update(stages(e.tools));e.router.register_worker(e.worker.actor_id,tuple(e.router.stages),principal=e.admin)
    source=e.catalog.get_asset(e.asset.asset_id,principal=e.owner)
    common=dict(asset_id=source.asset_id,source_sha256=source.sha256,expected_revision=source.revision,request_id='processing-export')
    spec=(ImageSubmission(**common,recipe=ImageRecipe(output_format='png',regions=(MaskRegion(region_id='face',purpose='face',box=(.2,.2,.8,.8),method='solid'),)))
        if masked else CleaningSubmission(**common,recipe=CleaningRecipe(source_sha256=source.sha256,content_kind='image')))
    service=ProcessingService(e.router,e.tools,options=options)
    job=service.request(spec,principal=e.owner)['job_id']
    assert LocalExecutor(e.router,e.worker,ProcessingHandlers(e.router,e.worker,e.tools,options=options)).run_once()['accepted']
    derived=ProcessingDerivedService(e.router,e.tools,options=options)
    status=derived.adopt(job,principal=e.owner)
    status=approve(derived,status,'privacy',e.owner);status=approve(derived,status,'quality',e.owner)
    e.asset=e.catalog.get_asset(status['asset_id'],principal=e.owner)
    obs=observation(e)
    if masked:decide(e,'含人脸',values=('已处理副本',),obs=obs)
    else:
        with pytest.raises(ValueError):decide(e,'含人脸',values=('已处理副本',),obs=obs)
    current=e.catalog.get_asset(status['asset_id'],principal=e.owner)
    export=e.exports.create(ExportSubmission(request_id='processing-csv',assets=(AssetSelection(asset_id=current.asset_id,expected_revision=current.revision,source_sha256=current.sha256),)),principal=e.owner)
    item=dict(zip(*rows(e.exports.content(export['export_id'],'assets.csv',principal=e.owner))))
    assert (item['含人脸']=='已处理副本') is masked
