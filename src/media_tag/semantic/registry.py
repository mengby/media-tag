from hashlib import sha256
from importlib.resources import files
import json
from pathlib import Path
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.models.contracts import ModelFailure
from media_tag.storage import StorageError


class SemanticRegistry:
    def __init__(self,root,*,manifest=None):
        self.root=Path(root)
        self.manifest=manifest or json.loads(files('media_tag.semantic').joinpath('model.lock.json').read_text(encoding='utf8'))
        self.revision=sha256(json.dumps(self.manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()

    def verify(self):
        backend=FilesystemBackend(self.root)
        try:
            for row in self.manifest['artifacts']:
                with backend.open_verified(row['path'],row['sha256'],row['size_bytes']):pass
        except (OSError,ValueError,RuntimeError,StorageError):raise ModelFailure('semantic_model_missing_or_changed') from None
        return self.root

    def fetch(self):
        from huggingface_hub import hf_hub_download
        self.root.mkdir(parents=True,exist_ok=True)
        for row in self.manifest['artifacts']:
            hf_hub_download(self.manifest['provider'],row['path'],revision=self.manifest['revision'],local_dir=self.root,token=False)
        self.verify()
        return {'state':'verified','model_revision':self.revision,'files':len(self.manifest['artifacts'])}
