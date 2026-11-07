import json
import pytest
from router.conftest import env
from upload.conftest import intake,tools
from media_tag.contracts.models import WorkerResult
from media_tag.router.runtime import LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.transforms.models import RetimeSubmission
from media_tag.transforms.runtime import RedactionHandlers,retime_stages
from media_tag.transforms.retime_service import RetimeService


@pytest.fixture
def retiming(intake,tools):
    env=intake
    env.router.stages.update(retime_stages(tools))
    for worker in env.workers:
        env.router.register_worker(worker.actor_id,tuple(env.router.stages),principal=env.admin)
    env.retimes=RetimeService(env.router,tools,reviewers=(env.owner.actor_id,))
    env.executor=LocalExecutor(env.router,env.workers[0],RedactionHandlers(env.retimes),after_release=env.retimes.adopt_ready)
    path=env.originals/'synthetic-retime.mp4'
    tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=size=96x64:rate=10:duration=2',
        '-c:v','libx264','-bf','0',str(path)],timeout=20)
    register(env,path)
    return env


def register(env,path):
    env.parent=env.catalog.register_file(FilesystemBackend(env.originals),path.name,source_backend_id='synthetic-retime',
        principal=env.owner,request_id=path.stem,name='Synthetic D11 timing source')
    env.parent_path=path
    return env.parent


def execute(env,**kwargs):
    request=RetimeSubmission(parent_asset_id=env.parent.asset_id,expected_revision=env.parent.revision,request_id='synthetic-retime',**kwargs)
    submitted=env.retimes.start(request,principal=env.owner)
    assert env.executor.run_once()['accepted']
    status=env.retimes.get(submitted['job_id'],principal=env.owner)
    assert status['job']['state']=='succeeded' and status['asset_id'],status
    asset=env.catalog.get_asset(status['asset_id'],principal=env.owner)
    with env.database.transaction() as connection:
        row=connection.execute('SELECT * FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?',(submitted['job_id'],)).fetchone()
        key=connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=?',(asset.sha256,)).fetchone()[0]
    result=WorkerResult.model_validate_json(row['result_json']); assert result.status=='succeeded'
    ref=next(a for a in result.artifacts if a.kind=='retime_report')
    report=json.loads((env.workspace/ref.ref).read_text())
    return submitted,status,asset,env.objects/key,report
