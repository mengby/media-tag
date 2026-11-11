from types import SimpleNamespace

import pytest
from PIL import Image

from router.conftest import env
from upload.conftest import tools, intake
from variants.conftest import variants
from media_tag.processing_queue import ProcessingDeployment, ProcessingHandlers, ProcessingService, stages
from media_tag.router.runtime import LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend


@pytest.fixture
def queue(env, tools):
    env.router.stages.update(stages(tools))
    worker = env.workers[0]
    env.router.register_worker(worker.actor_id, ('cleaning_output', 'image_mask_output'), principal=env.admin)
    options = ProcessingDeployment(enabled=True, timeout_seconds=60)
    handler = ProcessingHandlers(env.router, worker, tools, options=options)
    service = ProcessingService(env.router, tools, options=options)
    executor = LocalExecutor(env.router, worker, handler)
    return SimpleNamespace(env=env, tools=tools, options=options, handler=handler, service=service, executor=executor)


def register(queue, name='image.png'):
    path = queue.env.originals/name
    if not path.exists():
        Image.new('RGBA', (64, 48), (200, 100, 30, 123)).save(path)
    return queue.env.catalog.register_file(FilesystemBackend(queue.env.originals), name,
        source_backend_id='synthetic', principal=queue.env.owner, request_id=name, name=name)
