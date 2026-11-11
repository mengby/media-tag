import json

import pytest

from router.conftest import env
from upload.conftest import tools
from processing_queue.conftest import queue,register
from processing_queue.test_queue import run,submission
from processing_queue.test_derived import service,approve

from media_tag.cleaning import CleaningRecipe
from media_tag.cleaning.models import VideoSegment
from media_tag.files.service import FileError,FileService
from media_tag.processing_queue.derived_contracts import OutputReview
from media_tag.router.runtime import LocalExecutor
from media_tag.transforms.models import RetimeSubmission,ReviewSubmission
from media_tag.transforms.retime_service import RetimeService
from media_tag.transforms.runtime import RedactionHandlers,retime_stages
from media_tag.workers.probe import file_sha256


def test_approved_actual_cleaning_crop_is_old_retime_input_without_fake_annotations(queue):
    q=queue;e=q.env;path=e.originals/'crop-source.mp4'
    q.tools.ffmpeg_run(['-f','lavfi','-i','color=red:size=64x48:rate=10:duration=2',
        '-f','lavfi','-i','color=blue:size=64x48:rate=10:duration=6',
        '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]',
        '-c:v','libx264','-threads','1',str(path)],timeout=20)
    source=register(q,path.name);original_sha=file_sha256(path)
    recipe=CleaningRecipe(source_sha256=source.sha256,content_kind='video',segment=VideoSegment(start_seconds=2,end_seconds=6),preset='ultrafast')
    job,_=run(q,submission(q,source,recipe))
    derived=service(q);crop=derived.adopt(job,principal=e.owner)
    crop=approve(derived,crop,'quality',e.owner);crop=approve(derived,crop,'privacy',e.owner)
    assert crop['output_download_available']
    e.router.stages.update(retime_stages(q.tools))
    e.router.register_worker(e.workers[0].actor_id,('retime_render',),principal=e.admin)
    retiming=RetimeService(e.router,q.tools,reviewers=(e.owner.actor_id,))
    # No explicit segments: this is the ordinary old HTTP/UI request behavior.
    spec=RetimeSubmission(request_id='retime-cleaned-video',parent_asset_id=crop['asset_id'],
        expected_revision=crop['revision'],speed=2.0,audio='drop',chain_from_derived=True)
    requested=retiming.start(spec,principal=e.owner)
    executor=LocalExecutor(e.router,e.workers[0],RedactionHandlers(retiming),after_release=retiming.adopt_ready)
    assert executor.run_once()['accepted']
    output=retiming.get(requested['job_id'],principal=e.owner)
    repeated=retiming.start(spec,principal=e.owner)
    assert repeated['job_id']==requested['job_id'] and repeated['asset_id']==output['asset_id']
    assert executor.run_once() is None
    assert output['asset_id'] and output['source_sha256']==crop['output_sha256']
    assert output['cumulative_speed']==2.0 and output['segments']==[]
    with e.database.transaction() as connection:
        row=connection.execute('SELECT a.*,v.validation_json,p.spec_json FROM assets a JOIN variants v USING(asset_id) JOIN variant_plans p USING(plan_id) WHERE a.asset_id=?',(output['asset_id'],)).fetchone()
        validation=json.loads(row['validation_json']);contract=json.loads(row['spec_json'])
    assert row['parent_asset_id']==crop['asset_id'] and row['root_asset_id']==source.asset_id
    assert validation['duration_seconds']==pytest.approx(2,abs=.1)
    assert contract['time_map']['clock']['duration_seconds']==pytest.approx(4,abs=.1)
    assert contract['spec']['recipe']['segments']==[]
    files=FileService(e.router)
    with pytest.raises(FileError):files.open(output['asset_id'],row['blob_sha256'],principal=e.owner)
    review=ReviewSubmission(expected_revision=row['revision'],output_sha256=row['blob_sha256'],recipe_hash=output['recipe_hash'],
        decision='approved',full_output_viewed=True,exclusions_checked=True,risk_intervals_checked=True,
        note='Synthetic functional protocol test only; not a production privacy review')
    retiming.review(output['asset_id'],review,principal=e.owner)
    opened=files.open(output['asset_id'],row['blob_sha256'],principal=e.owner)
    try:
        raw,_=q.tools.ffmpeg_run(['-i',str(opened.stream.name),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],timeout=10)
        assert raw[2]>200 and raw[0]<30  # The real cropped blue frames survived retiming.
    finally:opened.close()
    crop_opened=files.open(crop['asset_id'],crop['output_sha256'],principal=e.owner);crop_opened.close()
    assert e.catalog.get_asset(crop['asset_id'],principal=e.owner).revision==crop['revision']
    assert file_sha256(path)==original_sha
    revoke=OutputReview(expected_revision=crop['revision'],output_sha256=crop['output_sha256'],recipe_hash=crop['recipe_hash'],dimension='quality',decision='revoked')
    derived.review(crop['asset_id'],revoke,principal=e.owner)
    with pytest.raises(FileError):files.open(output['asset_id'],row['blob_sha256'],principal=e.owner)
