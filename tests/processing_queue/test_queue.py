from hashlib import sha256
import json

import pytest
from PIL import Image
from pydantic import ValidationError

from media_tag.catalog import AccessDenied, Principal, RevisionConflict
from media_tag.cleaning import CleaningRecipe
from media_tag.cleaning.models import VideoSegment
from media_tag.image_processing import ImageRecipe, MaskRegion
from media_tag.processing_queue import CleaningSubmission, ImageSubmission, ProcessingService
from media_tag.workers.probe import file_sha256
from media_tag.contracts.models import WorkerResult
from media_tag.files.service import FileService, FileError
import threading

from .conftest import register


def submission(queue, asset, recipe, cls=CleaningSubmission, request_id='processing'):
    return cls(asset_id=asset.asset_id, expected_revision=asset.revision,
        source_sha256=asset.sha256, request_id=request_id, recipe=recipe)


def counts(queue):
    with queue.env.database.transaction() as connection:
        return connection.execute('SELECT count(*) FROM assets').fetchone()[0]


def output(queue, job_id):
    with queue.env.database.transaction() as connection:
        raw = connection.execute('SELECT a.result_json FROM router_attempts a JOIN router_tasks t ON t.task_id=a.task_id WHERE t.job_id=? AND a.lease_epoch=t.lease_epoch', (job_id,)).fetchone()[0]
    refs = json.loads(raw)['artifacts']
    return queue.env.workspace/next(item['ref'] for item in refs if item['kind']=='processing_output')


def run(queue, spec):
    accepted = queue.service.request(spec, principal=queue.env.owner)
    assert accepted['request_accepted'] and not accepted['processing_completed']
    queue.executor.run_once()
    return accepted['job_id'], queue.service.status(accepted['job_id'], principal=queue.env.owner)


def test_standard_copy_is_real_and_unregistered(queue):
    asset = register(queue)
    original = file_sha256(queue.env.originals/'image.png')
    before = counts(queue)
    job, status = run(queue, submission(queue, asset, CleaningRecipe(source_sha256=original, content_kind='image')))
    assert status['state']=='pending_registration_review', status
    assert status['processing_completed'] and status['output_sha256']==original
    assert file_sha256(output(queue, job))==original
    assert file_sha256(queue.env.originals/'image.png')==original and counts(queue)==before
    assert not status['asset_registered'] and not status['output_download_available']
    assert not status['privacy_confirmation'] and not status['quality_confirmation'] and not status['medical_confirmation']
    assert 'ref' not in json.dumps(status)


def test_real_image_mask_preserves_alpha_and_requires_review(queue):
    asset = register(queue)
    recipe = ImageRecipe(output_format='png', regions=(MaskRegion(region_id='face', purpose='face', box=(.25,.25,.75,.75), method='solid'),))
    before = counts(queue)
    job, status = run(queue, submission(queue, asset, recipe, ImageSubmission))
    assert status['state']=='pending_registration_review', status
    assert status['output_sha256']!=asset.sha256
    with Image.open(output(queue, job)) as image:
        assert image.getpixel((32,24))==(0,0,0,123)
        assert image.getpixel((0,0))==(200,100,30,123)
    assert counts(queue)==before and not status['privacy_confirmation']
    with pytest.raises(FileError):
        FileService(queue.env.router).authorize(asset.asset_id, status['output_sha256'], principal=queue.env.owner, for_bytes=True)


def test_real_video_crops_source_segment(queue):
    path = queue.env.originals/'clip.mp4'
    queue.tools.ffmpeg_run(['-f','lavfi','-i','color=red:size=64x48:rate=10:duration=2',
        '-f','lavfi','-i','color=blue:size=64x48:rate=10:duration=4',
        '-filter_complex','[0:v][1:v]concat=n=2:v=1:a=0[v]','-map','[v]',
        '-c:v','libx264','-threads','1','-pix_fmt','yuv420p',str(path)], timeout=20)
    asset = register(queue, 'clip.mp4')
    recipe = CleaningRecipe(source_sha256=asset.sha256, content_kind='video',
        segment=VideoSegment(start_seconds=2, end_seconds=6), target_duration_seconds=4, preset='ultrafast')
    job, status = run(queue, submission(queue, asset, recipe))
    assert status['state']=='pending_registration_review', status
    assert status['summary']['duration_target_state']=='in_3_to_5'
    raw, _ = queue.tools.ffmpeg_run(['-i',str(output(queue, job)),'-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'],timeout=10)
    assert raw[2]>200 and raw[0]<30
    assert file_sha256(path)==asset.sha256


def test_disabled_and_strict_input(queue):
    asset = register(queue)
    spec = submission(queue, asset, CleaningRecipe(source_sha256=asset.sha256, content_kind='image'))
    with pytest.raises(RevisionConflict):
        ProcessingService(queue.env.router, queue.tools).request(spec, principal=queue.env.owner)
    with pytest.raises(ValidationError):
        CleaningSubmission.model_validate(spec.model_dump()|{'path':'C:/private/file'})
    with pytest.raises(ValidationError):
        CleaningSubmission.model_validate(spec.model_dump()|{'source_sha256':'0'*64})


def test_edit_permission_and_compare_and_swap(queue):
    asset = register(queue)
    spec = submission(queue, asset, CleaningRecipe(source_sha256=asset.sha256, content_kind='image'))
    with pytest.raises(AccessDenied):
        queue.service.request(spec, principal=Principal(actor_id='stranger'))
    with pytest.raises(AccessDenied):
        queue.service.request(spec, principal=queue.env.workers[0])
    with pytest.raises(AccessDenied):
        queue.service.request(spec, principal=queue.env.admin)
    with pytest.raises(RevisionConflict):
        queue.service.request(spec.model_copy(update={'expected_revision':asset.revision+1}), principal=queue.env.owner)


def test_report_or_media_tamper_is_not_adopted(queue):
    asset = register(queue)
    job, status = run(queue, submission(queue, asset, CleaningRecipe(source_sha256=asset.sha256, content_kind='image')))
    assert status['processing_completed'], status
    output(queue, job).write_bytes(b'changed')
    after = queue.service.status(job, principal=queue.env.owner)
    assert after['state']=='processing_evidence_unavailable' and not after['processing_completed']


def test_real_animation_preserves_frames_and_timing(queue):
    path = queue.env.originals/'sticker.gif'
    frames = [Image.new('RGBA',(32,24),color) for color in ('red','blue')]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[100,200], loop=0)
    asset = register(queue, path.name)
    recipe = ImageRecipe(output_format='apng', regions=(MaskRegion(region_id='region',purpose='face', box=(.2,.2,.8,.8),method='solid'),))
    job, status = run(queue, submission(queue, asset, recipe, ImageSubmission))
    assert status['state']=='pending_registration_review', status
    assert status['summary']['animation_frame_count']==2
    with Image.open(output(queue,job)) as image:
        assert image.n_frames==2
        assert image.info['duration']==100
        image.seek(1)
        assert image.info['duration']==200
    assert file_sha256(path)==asset.sha256


def test_missing_audio_policy_remains_waiting(queue):
    path = queue.env.originals/'audio-video.mp4'
    queue.tools.ffmpeg_run(['-f','lavfi','-i','color=blue:size=64x48:rate=10:duration=4',
        '-f','lavfi','-i','sine=frequency=440:duration=4','-c:v','libx264','-threads','1',
        '-c:a','aac','-shortest',str(path)], timeout=20)
    asset = register(queue,path.name)
    recipe = CleaningRecipe(source_sha256=asset.sha256, content_kind='video',segment=VideoSegment(start_seconds=0,end_seconds=4))
    job, status = run(queue,submission(queue,asset,recipe))
    assert status['state']=='waiting_policy' and status['issue_code']=='audio_policy_required', status
    assert not status['processing_completed'] and status['output_sha256'] is None
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM router_tasks WHERE job_id=? AND state=?',(job,'needs_attention')).fetchone()[0]==1


def test_incomplete_animation_face_coverage_waits_for_policy(queue):
    path = queue.env.originals/'faces.gif'
    Image.new('RGB',(32,24),'red').save(path,save_all=True,append_images=[Image.new('RGB',(32,24),'blue')],duration=[100,100],loop=0)
    asset = register(queue,path.name)
    recipe = ImageRecipe(output_format='apng',regions=(MaskRegion(region_id='one-frame',purpose='face',box=(.2,.2,.8,.8),method='solid',frame_end=0),))
    job,status = run(queue,submission(queue,asset,recipe,ImageSubmission))
    assert status['state']=='waiting_policy',status
    assert status['issue_code']=='face_regions_must_cover_each_display_frame_including_poster'
    assert status['output_sha256'] is None


def test_source_cas_race_cancels_job(queue,monkeypatch):
    asset=register(queue)
    spec=submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image'))
    original=queue.env.router.submit
    def changed(*args,**kwargs):
        result=original(*args,**kwargs)
        queue.env.catalog.revise(asset.asset_id,principal=queue.env.owner,expected_revision=asset.revision,reason='test race',name='new name')
        return result
    monkeypatch.setattr(queue.env.router,'submit',changed)
    with pytest.raises(RevisionConflict):
        queue.service.request(spec,principal=queue.env.owner)
    assert queue.executor.run_once() is None
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT cancelled FROM router_jobs').fetchone()[0]==1


def test_changed_source_before_handler_has_no_adopted_output(queue):
    asset=register(queue)
    job=queue.service.request(submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')),principal=queue.env.owner)['job_id']
    queue.env.catalog.revise(asset.asset_id,principal=queue.env.owner,expected_revision=asset.revision,reason='changed source metadata',name='updated')
    queue.executor.run_once()
    status=queue.service.status(job,principal=queue.env.owner)
    assert status['state']=='source_changed' and not status['processing_completed']
    assert not list(queue.env.workspace.glob('**/output.png'))


def test_processing_status_waits_until_attempt_released(queue):
    asset=register(queue)
    job=queue.service.request(submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')),principal=queue.env.owner)['job_id']
    lease=queue.env.router.claim(principal=queue.env.workers[0])
    payload=queue.handler(lease,threading.Event())
    task=lease.task
    result=WorkerResult(task_id=task.task_id,attempt=task.attempt,lease_epoch=task.lease_epoch,input_fingerprint=task.input_fingerprint,versions=task.versions,elapsed_seconds=.5,**payload)
    assert queue.env.router.complete(result,principal=queue.env.workers[0])['accepted']
    assert not queue.service.status(job,principal=queue.env.owner)['processing_completed']
    queue.env.router.release(task.task_id,task.lease_epoch,principal=queue.env.workers[0])
    assert queue.service.status(job,principal=queue.env.owner)['state']=='pending_registration_review'


def test_cancelled_native_processing_never_adopts(queue,monkeypatch):
    asset=register(queue)
    job=queue.service.request(submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')),principal=queue.env.owner)['job_id']
    original=queue.handler.invoke
    def cancelled(request,folder,deadline,stopped):
        queue.env.router.cancel(job,principal=queue.env.owner)
        stopped.set()
        return original(request,folder,deadline,stopped)
    monkeypatch.setattr(queue.handler,'invoke',cancelled)
    queue.executor.run_once()
    status=queue.service.status(job,principal=queue.env.owner)
    assert status['state']=='cancelled' and not status['processing_completed']
    assert not list(queue.env.workspace.glob('**/output.png'))
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM router_attempts WHERE released_at IS NULL').fetchone()[0]==0


def test_explicit_edit_grant_is_checked_again_after_queueing(queue):
    asset=register(queue)
    actor=Principal(actor_id='collaborator')
    updated=queue.env.catalog.change_access(asset.asset_id,principal=queue.env.owner,expected_revision=asset.revision,
        subject_id=actor.actor_id,permission='edit',reason='synthetic grant')
    spec=submission(queue,updated,CleaningRecipe(source_sha256=asset.sha256,content_kind='image'))
    accepted=queue.service.request(spec,principal=actor)
    queue.executor.run_once()
    assert queue.service.status(accepted['job_id'],principal=actor)['state']=='pending_registration_review'
    queue.env.catalog.change_access(asset.asset_id,principal=queue.env.owner,expected_revision=updated.revision,
        subject_id=actor.actor_id,permission='edit',reason='synthetic revoke',revoke=True)
    with pytest.raises(AccessDenied):
        queue.service.status(accepted['job_id'],principal=actor)


def test_original_content_integrity_error_produces_no_output(queue):
    asset=register(queue)
    accepted=queue.service.request(submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')),principal=queue.env.owner)
    with queue.env.database.transaction() as connection:
        blob=connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=?',(asset.sha256,)).fetchone()
    (queue.env.objects/blob[0]).write_bytes(b'broken')
    queue.executor.run_once()
    status=queue.service.status(accepted['job_id'],principal=queue.env.owner)
    assert status['state']=='failed' and not status['processing_completed'],status
    assert not list(queue.env.workspace.glob('**/output.png'))


def test_unapproved_metadata_is_a_contract_invariant(queue):
    from media_tag.processing_queue.models import ProcessingReport
    asset=register(queue)
    job,status=run(queue,submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')))
    report=json.loads(next((queue.env.workspace/'jobs'/job).glob('**/processing.json')).read_text())
    for key,value in [('privacy_confirmation',True),('quality_confirmation',True),('medical_confirmation',True),
                      ('asset_registered',True),('output_download_available',True),('original_preserved',False),
                      ('effective_values_written',True),('license_status','approved')]:
        with pytest.raises(ValidationError):
            ProcessingReport.model_validate(report|{key:value})


def test_containment_assignment_failure_joins_gated_child(queue,monkeypatch):
    from media_tag.processing_queue import runtime
    from media_tag.models.contracts import ModelFailure
    import time
    actual=runtime.ProcessTree
    class RejectedTree(actual):
        def assign(self,process):
            raise ModelFailure('process_tree_containment_failed')
    monkeypatch.setattr(runtime,'ProcessTree',RejectedTree)
    asset=register(queue)
    started=time.monotonic()
    job,status=run(queue,submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image')))
    assert status['state']=='failed' and status['issue_code']=='process_tree_containment_failed',status
    assert time.monotonic()-started<5
    assert not list(queue.env.workspace.glob('**/start.ready'))
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM router_attempts WHERE released_at IS NULL').fetchone()[0]==0
