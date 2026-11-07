"""Only pinned local artifacts are loaded. Fetching is an explicit administrative command."""

from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path
import urllib.request
from uuid import uuid4

from media_tag.contracts.models import validate_relative_path
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.storage import StorageError
from .contracts import ModelFailure


class ModelRegistry:
    def __init__(self, root=Path('cache/models/d04'), *, manifest=None):
        self.root = Path(root)
        self.manifest = manifest or json.loads(files('media_tag.models').joinpath('models.lock.json').read_text(encoding='utf-8'))
        self.revision = sha256(json.dumps(self.manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def records(self, kind):
        records = [item for item in self.manifest['artifacts'] if item['path'].startswith(kind + '/')]
        if not records:
            raise ModelFailure('model_not_registered')
        return records

    def verify(self, kind):
        if not self.root.is_dir():
            raise ModelFailure('model_missing')
        backend = FilesystemBackend(self.root)
        paths = {}
        for item in self.records(kind):
            validate_relative_path(item['path'])
            try:
                with backend.open_verified(item['path'], item['sha256'], item['size_bytes']) as stream:
                    paths[Path(item['path']).name] = Path(stream.name)
            except StorageError:
                raise ModelFailure('model_missing_or_changed') from None
        return paths

    def fetch(self):
        self.root.mkdir(parents=True, exist_ok=True)
        backend = FilesystemBackend(self.root)
        for item in self.manifest['artifacts']:
            validate_relative_path(item['path'])
            destination = backend._resolve(item['path'], allow_missing=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = backend._resolve(item['path'], allow_missing=True)
            if destination.exists():
                with backend.open_verified(item['path'], item['sha256'], item['size_bytes']):
                    continue
            temporary = destination.parent / f'.fetch-{uuid4().hex}'
            try:
                digest, size = sha256(), 0
                with urllib.request.urlopen(item['url'], timeout=120) as response, temporary.open('xb') as target:
                    while chunk := response.read(1024 * 1024):
                        size += len(chunk)
                        if size > item['size_bytes']:
                            raise ModelFailure('model_download_size')
                        digest.update(chunk); target.write(chunk)
                if size != item['size_bytes'] or digest.hexdigest() != item['sha256']:
                    raise ModelFailure('model_download_integrity')
                # Destination is private pinned cache; do not overwrite a different existing artifact.
                import os
                os.link(temporary, destination)
            except (OSError, ValueError):
                raise ModelFailure('model_download_failed') from None
            finally:
                temporary.unlink(missing_ok=True)
        return {kind: self.verify(kind) for kind in ('ocr', 'face', 'clip')}
