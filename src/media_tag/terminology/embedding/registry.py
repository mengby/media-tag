"""Explicit download and full local verification of the pinned BGE artifact set."""
from hashlib import sha256
from importlib.resources import files
import json
import os
from pathlib import Path
import urllib.request
from uuid import uuid4
from media_tag.storage.filesystem import FilesystemBackend,require_local_path
from media_tag.storage import StorageError
from media_tag.models.contracts import ModelFailure


class BGERegistry:
    def __init__(self,root=Path('cache/models/d14'),*,manifest=None):
        self.root=Path(root)
        require_local_path(self.root)
        self.manifest=manifest or json.loads(files('media_tag.terminology.embedding').joinpath('bge.lock.json').read_bytes())
        self.revision=sha256(json.dumps(self.manifest,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()).hexdigest()

    def verify(self):
        if not self.root.is_dir():raise ModelFailure('text_model_missing')
        backend=FilesystemBackend(self.root);paths={}
        for item in self.manifest['artifacts']:
            try:
                with backend.open_verified(item['path'],item['sha256'],item['size_bytes']) as stream:paths[item['path']]=Path(stream.name)
            except StorageError:raise ModelFailure('text_model_missing_or_changed') from None
        return paths

    def fetch(self):
        self.root.mkdir(parents=True,exist_ok=True);backend=FilesystemBackend(self.root)
        for item in self.manifest['artifacts']:
            target=backend._resolve(item['path'],allow_missing=True)
            if target.exists():
                with backend.open_verified(item['path'],item['sha256'],item['size_bytes']):pass
                continue
            temporary=self.root/('.fetch-'+uuid4().hex)
            try:
                digest=sha256();size=0
                with urllib.request.urlopen(item['url'],timeout=45) as response,temporary.open('xb') as output:
                    while chunk:=response.read(1024*1024):
                        size+=len(chunk)
                        if size>item['size_bytes']:raise ModelFailure('text_model_download_size')
                        digest.update(chunk);output.write(chunk)
                    output.flush();os.fsync(output.fileno())
                if size!=item['size_bytes'] or digest.hexdigest()!=item['sha256']:raise ModelFailure('text_model_download_integrity')
                os.link(temporary,target)
            except OSError:raise ModelFailure('text_model_download_failed') from None
            finally:temporary.unlink(missing_ok=True)
        self.verify()
        return {'model_revision':self.revision,'upstream_revision':self.manifest['upstream_revision'],'verified':True}
