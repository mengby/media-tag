from hashlib import sha256
import pytest
from media_tag.upload.models import UploadSpec
from media_tag.review.models import Observation
from .conftest import current,observation,decide


def video(env):
    path=env.originals/'synthetic-motion.mp4'
    env.tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=size=96x64:rate=8:duration=1','-c:v','libx264','-bf','0',str(path)],timeout=20)
    raw=path.read_bytes();session=env.uploads.create(UploadSpec(request_id='motion',filename=path.name,size_bytes=len(raw),sha256=sha256(raw).hexdigest()),principal=env.owner)
    env.uploads.append(session['upload_id'],0,raw,sha256(raw).hexdigest(),principal=env.owner)
    uploaded=env.uploads.complete(session['upload_id'],principal=env.owner)
    assert env.executor.run_once()['accepted'] and env.executor.run_once()['accepted']
    env.asset=env.catalog.get_asset(uploaded['asset_id'],principal=env.owner)
    env.catalog.change_access(env.asset.asset_id,principal=env.owner,expected_revision=1,subject_id=env.reviewer.actor_id,permission='edit',reason='Synthetic video review scope')


def test_continuous_intervals_dynamic_fields_and_face_absence_scope(reviewed):
    env=reviewed;video(env)
    with pytest.raises(ValueError):observation(env,kind='image')
    with pytest.raises(ValueError):observation(env,kind='video_segment',start_seconds=.8,end_seconds=2.)
    short=observation(env,kind='video_segment',start_seconds=0.,end_seconds=.5)
    with pytest.raises(ValueError):decide(env,'含人脸',values=('无',),obs=short)
    whole=observation(env,kind='video_segment',start_seconds=0.,end_seconds=1.)
    decide(env,'含人脸',values=('无',),obs=whole)
    expert=observation(env,actor=env.reviewer,kind='video_segment',start_seconds=0.,end_seconds=1.)
    decide(env,'过程/状态',values=('合成测试方向A→B',),actor=env.reviewer,obs=expert,dynamic=True)
    with pytest.raises(ValueError):decide(env,'含人脸',values=('已处理副本',),obs=whole)
