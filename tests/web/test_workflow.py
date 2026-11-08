import json
from media_tag.web.models import Enrollment
from media_tag.router.models import JobSpec
from media_tag.contracts.models import WorkerResult
from retime.conftest import execute
from router.conftest import step, finish, submit


def test_retime_download_native_ranges_explicit_enrollment_and_version_folding(retime_web):
    env=retime_web
    _,status,asset,path,report=execute(env,speed=2)
    assert env.web.assets(principal=env.owner)['items']==[]
    response=env.clients.owner.get(f'/ui/media/{asset.asset_id}?sha256={asset.sha256}',headers={'Range':'bytes=0-49'})
    assert response.status_code==206 and response.content==path.read_bytes()[:50]
    result=env.web.enroll(asset.asset_id,Enrollment(request_id='output-enroll',expected_revision=1,output_sha256=asset.sha256,recipients=(env.reader.actor_id,)),principal=env.owner)
    listing=env.clients.reader.get('/ui/library').json()['items']
    assert len(listing)==1 and len(listing[0]['versions'])==1
    assert listing[0]['versions'][0]['asset_id']==asset.asset_id
    assert env.clients.reader.get(f'/ui/media/{env.parent.asset_id}?sha256={env.parent.sha256}').status_code==403
    assert env.clients.reader.get(f'/ui/thumbnail/{asset.asset_id}?sha256={asset.sha256}').status_code==404
    assert env.clients.reader.get(f'/ui/media/{asset.asset_id}?sha256={asset.sha256}&download=true').status_code==200
    assert result['job_id'] and report['speed']==2


def test_retry_only_failed_stages_with_cas_and_auth(personnel):
    env=personnel
    from router.conftest import Config
    from media_tag.router.models import StageDefinition
    env.router.stages['synthetic']=StageDefinition(Config,'synthetic-v1',synthetic=True)
    for w in env.workers:env.router.register_worker(w.actor_id,tuple(env.router.stages),principal=env.admin)
    job=submit(env,steps=(step('first'),step('second',depends_on=('first',))))
    first=env.router.claim(principal=env.workers[0]);finish(env,first)
    second=env.router.claim(principal=env.workers[0])
    result=WorkerResult(task_id=second.task.task_id,attempt=1,lease_epoch=1,input_fingerprint=second.task.input_fingerprint,
        versions=second.task.versions,status='failed',elapsed_seconds=.1,errors=({'code':'synthetic','message':'synthetic','retryable':False},))
    env.router.complete(result,principal=env.workers[0]);env.router.release(second.task.task_id,1,principal=env.workers[0])
    assert env.clients.stranger.post('/ui/tasks/'+job['job_id']+'/retry',json={'expected_revision':1}).status_code==403
    response=env.clients.owner.post('/ui/tasks/'+job['job_id']+'/retry',json={'expected_revision':1})
    assert response.status_code==200
    current=response.json()
    assert next(t for t in current['tasks'] if t['key']=='first')['task_id']==first.task.task_id
    assert next(t for t in current['tasks'] if t['key']=='second')['task_id']!=second.task.task_id
    assert env.clients.owner.post('/ui/tasks/'+job['job_id']+'/retry',json={'expected_revision':1}).status_code==409


def test_failed_background_analysis_does_not_block_download_or_processing(retime_web):
    env=retime_web
    _,_,asset,path,_=execute(env,speed=2)
    entry=env.web.enroll(asset.asset_id,Enrollment(request_id='synthetic-analysis-failure',expected_revision=1,
        output_sha256=asset.sha256),principal=env.owner)
    from media_tag.router.runtime import LocalExecutor
    from media_tag.workers.probe import MediaError
    def fail(lease,stopped):raise MediaError('decode_failed','Synthetic background-stage failure; no successful model claim')
    worker=LocalExecutor(env.router,env.workers[0],fail)
    assert worker.run_once()['accepted']
    status=env.router.get_job(entry['job_id'],principal=env.owner)
    assert any(t['state']=='needs_attention' for t in status['tasks'])
    assert env.clients.owner.get(f'/ui/media/{asset.asset_id}?sha256={asset.sha256}&download=true').content==path.read_bytes()
    from media_tag.transforms.models import RetimeSubmission
    run=env.retimes.start(RetimeSubmission(request_id='after-analysis-failure',parent_asset_id=env.parent.asset_id,
        expected_revision=1,speed=1.25),principal=env.owner)
    assert env.executor.run_once()['accepted']
    assert env.retimes.get(run['job_id'],principal=env.owner)['asset_id']
