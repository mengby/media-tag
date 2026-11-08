from hashlib import sha256
from io import BytesIO
import json
import pytest
from media_tag.models.contracts import ModelFailure
from media_tag.storage import StorageError
from media_tag.terminology.embedding.registry import BGERegistry


def test_download_only_explicit_and_existing_corruption_never_overwritten(tmp_path,monkeypatch):
    raw=b'synthetic bytes, not real weights'
    manifest={'artifacts':[{'path':'synthetic.bin','sha256':sha256(raw).hexdigest(),'size_bytes':len(raw),'url':'https://synthetic.invalid/model'}],'upstream_revision':'synthetic'}
    registry=BGERegistry(tmp_path/'model',manifest=manifest)
    with pytest.raises(ModelFailure):registry.verify()
    calls=[]
    def fetch(*args,**kwargs):calls.append(args[0]);return BytesIO(raw)
    monkeypatch.setattr('urllib.request.urlopen',fetch)
    assert registry.fetch()['verified'] and len(calls)==1
    assert registry.fetch()['verified'] and len(calls)==1
    (registry.root/'synthetic.bin').write_bytes(b'changed')
    with pytest.raises(ModelFailure):registry.verify()
    with pytest.raises(StorageError):registry.fetch()
    assert (registry.root/'synthetic.bin').read_bytes()==b'changed'


def test_upstream_lock_is_immutable_safe_and_complete():
    registry=BGERegistry();manifest=registry.manifest
    assert manifest['dimension']==512 and manifest['pooling']=='cls' and manifest['normalize']
    assert manifest['upstream_revision']=='7999e1d3359715c523056ef9478215996d62a620'
    assert {a['path'] for a in manifest['artifacts']}=={'README.md','config.json','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt','model.safetensors'}
    for item in manifest['artifacts']:assert manifest['upstream_revision'] in item['url'] and len(item['sha256'])==64
