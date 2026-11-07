import json
import pytest
from PIL import Image
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.transforms.models import ScanSubmission,RevisionSubmission
from media_tag.contracts.models import WorkerResult
from .test_pipeline import rendering


@pytest.mark.parametrize('kind',['vfr','rotated','offset','audio-offset'])
def test_actual_full_video_timestamps_rotation_and_audio_are_preserved(redaction,tools,kind):
    env=redaction; target=env.originals/f'synthetic-{kind}.mp4'
    if kind=='vfr':
        tools.ffmpeg_run(['-v','error','-i',str(env.parent_path),'-vf',"setpts=if(lt(N\\,3)\\,N*0.25/TB\\,(0.75+(N-3)*0.5)/TB)",
            '-fps_mode','vfr','-c:v','libx264','-bf','0',str(target)],timeout=20)
    elif kind=='rotated':
        tools.ffmpeg_run(['-v','error','-display_rotation','90','-i',str(env.parent_path),'-c','copy',str(target)],timeout=20)
    elif kind=='offset':
        tools.ffmpeg_run(['-v','error','-i',str(env.parent_path),'-c','copy','-output_ts_offset','5',str(target)],timeout=20)
    else:
        tools.ffmpeg_run(['-v','error','-i',str(env.parent_path),'-itsoffset','0.25','-f','lavfi','-i','sine=frequency=400:sample_rate=8000:duration=1.5',
            '-map','0:v','-map','1:a','-c:v','copy','-c:a','aac',str(target)],timeout=20)
    env.parent=env.catalog.register_file(FilesystemBackend(env.originals),target.name,source_backend_id='synthetic-timing',
        principal=env.owner,request_id=kind,name='Synthetic timing sample')
    env.parent_path=target; before=target.read_bytes()
    job=env.redactions.start_scan(ScanSubmission(request_id=kind,parent_asset_id=env.parent.asset_id,expected_revision=1),principal=env.owner)
    assert env.executor.run_once()['accepted']
    with env.database.transaction() as connection:
        row=connection.execute('SELECT * FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?',(job['job_id'],)).fetchone()
    result=WorkerResult.model_validate_json(row['result_json']); assert result.status=='succeeded',result
    artifact=next(a for a in result.artifacts if a.kind=='face_tracks')
    report=json.loads((env.workspace/artifact.ref).read_text())
    if kind=='vfr': assert len({round(f['duration'],3) for f in report['frames']})>1
    if kind=='rotated': assert report['source_display']==[400,640]
    if kind=='offset': assert report['source_clock']['origin_seconds']==pytest.approx(5)
    env.scan_spec=RevisionSubmission(scan_task_id=row['task_id'],lease_epoch=row['lease_epoch'],report_sha256=artifact.sha256)
    env.revision=env.redactions.create_revision(env.scan_spec,principal=env.owner)
    run,asset=rendering(env)
    with env.database.transaction() as connection:
        variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(asset.asset_id,)).fetchone()
    validation=json.loads(variant['validation_json'])
    assert validation['has_audio']==(kind=='audio-offset') and validation['intake_decode']=='full'
    assert target.read_bytes()==before
