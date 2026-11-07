from hashlib import sha256
from io import BytesIO

from PIL import Image
import pytest

from router.conftest import env
from media_tag.router.runtime import cpu_stages
from media_tag.upload.models import UploadSpec, UploadOptions
from media_tag.upload.service import UploadService
from media_tag.workers.probe import Toolchain


@pytest.fixture(scope='session')
def tools():
    return Toolchain()


@pytest.fixture
def intake(env, tools):
    env.router.stages = cpu_stages(tools)
    for worker in env.workers:
        env.router.register_worker(worker.actor_id, ('probe', 'evidence'), principal=env.admin)
    env.uploads = UploadService(env.router, tools, options=UploadOptions(chunk_bytes=1024), clock=lambda: env.now[0])
    output = BytesIO(); Image.new('RGB', (32, 24), 'red').save(output, 'PNG')
    env.data = output.getvalue()
    return env


def spec(env, *, request='synthetic-upload', **kwargs):
    return UploadSpec(request_id=request, filename='合成图片.png', size_bytes=len(env.data), sha256=sha256(env.data).hexdigest(), **kwargs)


def append(env, upload, data=None, offset=0, principal=None):
    data = env.data if data is None else data
    return env.uploads.append(upload['upload_id'], offset, data, sha256(data).hexdigest(), principal=principal or env.owner)


def upload(env, *, request='synthetic-upload', principal=None, **kwargs):
    principal = principal or env.owner
    session = env.uploads.create(spec(env, request=request, **kwargs), principal=principal)
    append(env, session, principal=principal)
    return env.uploads.complete(session['upload_id'], principal=principal)
