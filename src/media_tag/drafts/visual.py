"""Explicit local CPU text encoding, bounded and isolated from GPU workers."""
from collections import OrderedDict
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from uuid import uuid4
from media_tag.workers.evidence import _directory
from .service import vector


class VisualEncoder:
    def __init__(self,registry,workspace):
        self.registry,self.workspace=registry,workspace;self.lock=threading.Lock();self.cache=OrderedDict()

    @contextmanager
    def _turn(self):
        if not self.lock.acquire(timeout=1):raise ValueError('visual query busy')
        try:yield
        finally:self.lock.release()

    def encode(self,text):
        # No model load inside a SQLite transaction; one CPU child at a time.
        with self._turn():
            if text in self.cache:self.cache.move_to_end(text);return self.cache[text]
            folder=_directory(self.workspace,'jobs/visual-query-'+uuid4().hex)
            (folder/'README.md').write_text('Private local CPU visual-query text encoding; no external call or medical approval.\n',encoding='utf8')
            request=folder/'request.json';response=folder/'response.json'
            request.write_text(json.dumps({'text':text,'model_root':str(self.registry.root.resolve()),'model_revision':self.registry.revision,'executor_pid':os.getpid()}),encoding='utf8')
            env=os.environ.copy();env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',OMP_NUM_THREADS='1',TOKENIZERS_PARALLELISM='false',PYTHONUTF8='1')
            subprocess.run([sys.executable,'-m','media_tag.drafts.visual',str(request),str(response)],env=env,
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=45,check=True,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            raw=response.read_bytes()
            if len(raw)>32768:raise ValueError('visual response budget')
            result=json.loads(raw)
            if result.get('model_revision')!=self.registry.revision:raise ValueError('visual model mismatch')
            value=vector(result['embedding']);self.cache[text]=value
            if len(self.cache)>128:self.cache.popitem(last=False)
            return value


def main():
    request=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
    from media_tag.models.lifecycle import watch_parent
    watch_parent(request['executor_pid'])
    from media_tag.models.registry import ModelRegistry
    from media_tag.models.inference import CLIPModel
    registry=ModelRegistry(Path(request['model_root']))
    if registry.revision!=request['model_revision'] or not isinstance(request['text'],str) or not 0<len(request['text'])<=2000:raise ValueError('visual request')
    model=CLIPModel(registry,device='cpu');torch=model.torch
    tokens=model.processor(text=[request['text']],padding=True,truncation=False,return_tensors='pt')
    if tokens['input_ids'].shape[1]>52:raise ValueError('visual query token budget')
    with torch.inference_mode():
        output=model.model.text_model(**tokens,return_dict=True)
        features=model.model.text_projection(output.last_hidden_state[:,0,:])
        features=torch.nn.functional.normalize(features.float(),dim=-1)
    result={'model_revision':registry.revision,'embedding':vector(features[0].cpu().tolist())}
    Path(sys.argv[2]).write_text(json.dumps(result,allow_nan=False),encoding='utf8')


if __name__=='__main__':main()
