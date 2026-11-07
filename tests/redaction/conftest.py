from pathlib import Path
import json
from PIL import Image
import pytest
from router.conftest import env
from upload.conftest import intake,tools
from media_tag.catalog import Principal
from media_tag.contracts.models import WorkerResult
from media_tag.models.registry import ModelRegistry
from media_tag.router.runtime import LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.transforms.models import ScanSubmission,RevisionSubmission
from media_tag.transforms.runtime import RedactionHandlers,redaction_stages
from media_tag.transforms.service import RedactionService


@pytest.fixture
def redaction(intake,tools):
    env=intake
    registry=ModelRegistry(Path('cache/models/d04').resolve())
    env.router.stages.update(redaction_stages(registry,tools))
    for worker in env.workers:
        env.router.register_worker(worker.actor_id,tuple(env.router.stages),principal=env.admin)
    env.reviewer=Principal(actor_id='synthetic-reviewer')
    env.redactions=RedactionService(env.router,tools,registry,reviewers=(env.reviewer.actor_id,))
    sample=Path('cache/models/d04/public-face-sample.jpg')
    from media_tag.workers.probe import file_sha256
    assert file_sha256(sample)=='7de7ed51a1594fff247f4cae2301eceacf5313d6011e37b4a4c8733f7bb72c07'
    image=Image.new('RGB',(640,400),'white')
    with Image.open(sample) as face:
        image.paste(face.resize((300,300)),(40,50)); image.paste(face.resize((250,250)),(370,100))
    still=env.originals/'synthetic-faces.png'; image.save(still)
    source=env.originals/'synthetic-faces.mp4'
    tools.ffmpeg_run(['-v','error','-loop','1','-framerate','4','-i',str(still),'-t','1.5','-c:v','libx264','-pix_fmt','yuv420p',str(source)],timeout=20)
    env.parent=env.catalog.register_file(FilesystemBackend(env.originals),source.name,source_backend_id='synthetic-faces',
        principal=env.owner,request_id='synthetic-faces',name='Synthetic public diagnostic face composition')
    env.parent_path=source
    env.executor=LocalExecutor(env.router,env.workers[0],RedactionHandlers(env.redactions),after_release=env.redactions.adopt_ready)
    return env


@pytest.fixture
def scanned(redaction):
    env=redaction
    job=env.redactions.start_scan(ScanSubmission(request_id='synthetic-scan',parent_asset_id=env.parent.asset_id,expected_revision=1),principal=env.owner)
    assert env.executor.run_once()['accepted']
    status=env.router.get_job(job['job_id'],principal=env.owner)
    assert status['state']=='succeeded',status
    with env.database.transaction() as connection:
        row=connection.execute('SELECT * FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?',(job['job_id'],)).fetchone()
    result=WorkerResult.model_validate_json(row['result_json']); report=next(a for a in result.artifacts if a.kind=='face_tracks')
    env.scan_spec=RevisionSubmission(scan_task_id=row['task_id'],lease_epoch=row['lease_epoch'],report_sha256=report.sha256)
    env.scan_report=json.loads((env.workspace/report.ref).read_text())
    env.revision=env.redactions.create_revision(env.scan_spec,principal=env.owner)
    return env
