from hashlib import sha256
from pathlib import Path

from precision.conftest import env, precise, sought, tools
from PIL import Image

from media_tag.api.config import LocalConfig, executor_from_config
from media_tag.similarity.pipeline import Deployment
from media_tag.similarity.query import DescriptorQuery
from media_tag.storage.filesystem import FilesystemBackend


def test_default_disabled_and_actual_configured_worker(precise, tmp_path):
    p = precise
    config = LocalConfig(workspace=str(p.env.workspace), database=str(p.env.database.path),
        object_store=str(p.env.catalog.object_store.root), template_dir=str((Path.cwd()/'asset-index-template').resolve()),
        template_fingerprint=p.env.contract.fingerprint, credentials={sha256(b'owner').hexdigest(): p.env.owner,
        sha256(b'worker').hexdigest(): p.env.worker}, workers=(p.env.worker.actor_id,), tool_dir=str(p.tools.ffmpeg.parent),
        analysis=p.analysis.options)
    assert not config.similarity.enabled
    path = tmp_path/'synthetic-similarity.local.json'
    config = config.model_copy(update={'similarity': Deployment(enabled=True)})
    path.write_text(config.model_dump_json(), encoding='utf8')
    executor = executor_from_config(path, p.env.worker.actor_id)
    originals = tmp_path/'new-originals'
    originals.mkdir(); (originals/'README.md').write_text('Synthetic image intake, original read only.\n')
    Image.new('RGB', (48, 48), 'green').save(originals/'new.png')
    asset = executor.router.catalog.register_file(FilesystemBackend(originals), 'new.png',
        source_backend_id='synthetic-config', principal=p.env.owner, request_id='new-config-image', name='new')
    for _ in range(12):
        if executor.run_once() is None:
            break
    assert DescriptorQuery(executor.router).status(asset.asset_id, principal=p.env.owner)['state'] == 'ready'
