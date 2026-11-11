from hashlib import sha256
from pathlib import Path
import pytest
from media_tag.contracts.models import WorkerResult
from media_tag.transforms.models import RenderSubmission
from web.conftest import clients
from redaction.conftest import redaction, scanned


def test_processing_visual_derivative_does_not_break_legacy_review_queue(scanned):
    from PIL import Image
    from media_tag.cleaning import CleaningRecipe
    from media_tag.processing_queue import CleaningSubmission,ProcessingDeployment,ProcessingHandlers,ProcessingService,stages
    from media_tag.processing_queue.derived import ProcessingDerivedService
    from media_tag.router.runtime import LocalExecutor
    from media_tag.storage.filesystem import FilesystemBackend
    env=scanned;tools=env.uploads.tools
    env.redactions.reviewers=frozenset((env.owner.actor_id,))
    rendered=env.redactions.start_render(RenderSubmission(request_id='legacy-output-with-new-contract',revision_hash=env.revision['revision_hash']),principal=env.owner)
    assert env.executor.run_once()['accepted']
    legacy=env.redactions.get_run(rendered['run_id'],principal=env.owner)['asset_id']
    Image.new('RGBA',(32,24),(100,60,20,120)).save(env.originals/'processing-ui.png')
    source=env.catalog.register_file(FilesystemBackend(env.originals),'processing-ui.png',source_backend_id='synthetic-ui',
        principal=env.owner,request_id='new-processing-ui-source',name='Synthetic processing image')
    options=ProcessingDeployment(enabled=True)
    env.router.stages.update(stages(tools));env.router.register_worker(env.workers[0].actor_id,tuple(env.router.stages),principal=env.admin)
    processing=ProcessingService(env.router,tools,options=options)
    accepted=processing.request(CleaningSubmission(request_id='new-processing-ui',asset_id=source.asset_id,
        source_sha256=source.sha256,expected_revision=source.revision,recipe=CleaningRecipe(source_sha256=source.sha256,content_kind='image')),principal=env.owner)
    executor=LocalExecutor(env.router,env.workers[0],ProcessingHandlers(env.router,env.workers[0],tools,options=options))
    assert executor.run_once()['accepted']
    derived=ProcessingDerivedService(env.router,tools,options=options).adopt(accepted['job_id'],principal=env.owner)
    assert derived['reviews']['privacy']=='pending'
    clients(env,redactions=env.redactions,processing=processing)
    response=env.clients.owner.get('/ui/review-queue')
    assert response.status_code==200,response.text
    ids={version['asset_id'] for group in response.json()['items'] for version in group['versions']}
    assert legacy in ids and derived['asset_id'] not in ids


def test_private_scan_frames_output_review_sharing_thumbnail_and_revocation(scanned):
    env=scanned
    env.redactions.reviewers=frozenset((env.owner.actor_id,'synthetic-stranger'))  # Explicitly synthetic test reviewer.
    clients(env,redactions=env.redactions)
    owner,reader,stranger=env.clients.owner,env.clients.reader,env.clients.stranger
    with env.database.transaction() as connection:
        scan_job=connection.execute('SELECT job_id FROM router_tasks WHERE task_id=?',(env.scan_spec.scan_task_id,)).fetchone()[0]
    frame=owner.get(f'/ui/scans/{scan_job}/frames/0')
    assert frame.status_code==200 and sha256(frame.content).hexdigest()==env.scan_report['frames'][0]['decoded_sha256']
    assert stranger.get(f'/ui/scans/{scan_job}/frames/0').status_code==403
    assert owner.get(f'/ui/scans/{scan_job}/frames/99999').status_code==404
    submitted=env.redactions.start_render(RenderSubmission(request_id='synthetic-ui-render',revision_hash=env.revision['revision_hash']),principal=env.owner)
    assert env.executor.run_once()['accepted']
    done=env.redactions.get_run(submitted['run_id'],principal=env.owner)
    aid=done['asset_id'];details=owner.get('/v1/assets/'+aid).json()
    payload={'request_id':'pending','expected_revision':1,'output_sha256':details['sha256'],'recipients':[env.reader.actor_id]}
    assert owner.post('/ui/library/'+aid,json=payload).status_code==409
    assert owner.get(f'/ui/media/{aid}?sha256={details["sha256"]}&download=true').status_code==409
    assert owner.get(f'/ui/media/{aid}?sha256={details["sha256"]}&review=redaction&download=true').status_code==409
    assert owner.get(f'/ui/media/{aid}?sha256={details["sha256"]}&review=redaction',headers={'Range':'bytes=0-30'}).status_code==206
    assert stranger.get(f'/ui/media/{aid}?sha256={details["sha256"]}&review=redaction').status_code==403
    assert owner.get(f'/ui/thumbnail/{aid}?sha256={details["sha256"]}').status_code==409
    assert stranger.get('/ui/review-queue').json()['items']==[]
    assignment={'expected_revision':1,'output_sha256':details['sha256'],'subject_id':env.stranger.actor_id}
    assert owner.post('/ui/outputs/'+aid+'/assign-reviewer?kind=redaction',json=assignment|{'subject_id':env.reader.actor_id}).status_code==422
    assert owner.post('/ui/outputs/'+aid+'/assign-reviewer?kind=redaction',json=assignment).status_code==200
    queue=stranger.get('/ui/review-queue').json()['items']
    assert queue[0]['versions'][0]['asset_id']==aid
    assert stranger.get(f'/ui/media/{aid}?sha256={details["sha256"]}&review=redaction').status_code==200
    assert stranger.get(f'/ui/media/{aid}?sha256={details["sha256"]}&download=true').status_code==409
    assert stranger.post('/ui/outputs/'+aid+'/assign-reviewer?kind=redaction',json=assignment|{'expected_revision':2}).status_code==403
    review={'expected_revision':2,'output_sha256':details['sha256'],'recipe_hash':details['transform']['recipe_hash'],
            'decision':'approved','full_output_viewed':True,'exclusions_checked':True,'risk_intervals_checked':True,'note':'Synthetic engineering test only; not a production review'}
    wrong=review|{'output_sha256':'0'*64}
    assert owner.post(f'/redaction/outputs/{aid}/review',json=wrong).status_code==412
    assert owner.post(f'/redaction/outputs/{aid}/review',json=review).status_code==200
    payload.update(request_id='approved',expected_revision=3)
    entry=owner.post('/ui/library/'+aid,json=payload)
    assert entry.status_code==200 and entry.json()['job_id']
    assert reader.get(f'/ui/media/{env.parent.asset_id}?sha256={env.parent.sha256}').status_code==403
    assert reader.get(f'/ui/media/{aid}?sha256={details["sha256"]}&download=true').status_code==200
    # Thumbnail is absent until output-specific analysis; it cannot fall back to the source frame.
    assert reader.get(f'/ui/thumbnail/{aid}?sha256={details["sha256"]}').status_code==404
    from media_tag.router.runtime import LocalExecutor,CPUHandlers
    cpu=LocalExecutor(env.router,env.workers[0],CPUHandlers(env.router,env.workers[0],env.uploads.tools))
    assert cpu.run_once()['accepted'] and cpu.run_once()['accepted']
    thumb=reader.get(f'/ui/thumbnail/{aid}?sha256={details["sha256"]}')
    assert thumb.status_code==200 and thumb.content!=frame.content
    with env.database.transaction() as connection:
        records,_=env.web.files._previews(connection,aid,details['sha256'])
        assert records and records[0]['artifact'].sha256==sha256(thumb.content).hexdigest()
    # A real background probe started while approved must not be adopted after revocation.
    from media_tag.router.models import JobSpec,StepSpec
    with env.database.transaction() as connection:
        prior=connection.execute("SELECT spec_json FROM router_tasks WHERE job_id=? AND step_key='probe'",(entry.json()['job_id'],)).fetchone()
    probe=StepSpec.model_validate_json(prior[0])
    follow=env.router.submit(JobSpec(asset_id=aid,steps=(probe,)),request_id='synthetic-probe-before-revocation',principal=env.owner)
    lease=env.router.claim(principal=env.workers[0]);assert lease and lease.task.job_id==follow['job_id']
    import threading
    cpu_payload=CPUHandlers(env.router,env.workers[0],env.uploads.tools)(lease,threading.Event())
    outcome=WorkerResult(task_id=lease.task.task_id,attempt=lease.task.attempt,lease_epoch=lease.task.lease_epoch,
        input_fingerprint=lease.task.input_fingerprint,versions=lease.task.versions,elapsed_seconds=.1,**cpu_payload)
    assert owner.post(f'/redaction/outputs/{aid}/review',json=review|{'expected_revision':4,'decision':'revoked'}).status_code==200
    assert env.router.complete(outcome,principal=env.workers[0])['accepted'] is False
    env.router.release(lease.task.task_id,lease.task.lease_epoch,principal=env.workers[0])
    assert reader.get(f'/ui/thumbnail/{aid}?sha256={details["sha256"]}').status_code==409
    assert reader.get(f'/ui/media/{aid}?sha256={details["sha256"]}&download=true').status_code==409
    assert reader.get('/ui/library').json()['items']==[]
    assert owner.post('/ui/library/'+aid,json=payload|{'request_id':'cannot-republish','expected_revision':5}).status_code==409
