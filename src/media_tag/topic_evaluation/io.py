from hashlib import sha256
import json
from pathlib import Path

from media_tag.storage.filesystem import FilesystemBackend


class EvaluationError(ValueError):pass


def strict_json(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise EvaluationError('duplicate_json_key')
            result[key]=value
        return result
    def reject(value):raise EvaluationError('nonfinite_json_number')
    return json.loads(raw,object_pairs_hook=unique,parse_constant=reject)


def load(path,model,*,reader=None,return_raw=False):
    path=Path(path)
    backend=FilesystemBackend(path.parent)
    with backend.open_read(path.name) as stream:raw=(reader(stream) if reader else stream).read(8*1024**2+1)
    if len(raw)>8*1024**2:raise EvaluationError('import_byte_budget')
    strict_json(raw)
    result=(model.model_validate_json(raw),sha256(raw).hexdigest())
    return (*result,raw) if return_raw else result


class Bundle:
    def __init__(self,root,*,max_file_bytes=16*1024**2,max_total_bytes=256*1024**2):
        if not 0<max_file_bytes<=16*1024**2 or not 0<max_total_bytes<=256*1024**2:
            raise ValueError('bounded_artifact_read_budget_required')
        self.backend=FilesystemBackend(Path(root));self.cache={};self.failed=set();self.bytes_read=0
        self.max_file_bytes,self.max_total_bytes=max_file_bytes,max_total_bytes
    def read(self,ref):
        key=(ref.ref,ref.sha256)
        if key in self.cache:return self.cache[key]
        if key in self.failed:raise EvaluationError('artifact_unavailable_cached')
        try:
            remaining=self.max_total_bytes-self.bytes_read
            if remaining<=0:raise EvaluationError('artifact_byte_budget')
            with self.backend.open_read(ref.ref) as stream:raw=stream.read(min(self.max_file_bytes+1,remaining))
            self.bytes_read+=len(raw)
            if len(raw)>self.max_file_bytes or self.bytes_read>=self.max_total_bytes:
                raise EvaluationError('artifact_byte_budget')
            if sha256(raw).hexdigest()!=ref.sha256:raise EvaluationError('artifact_integrity')
            self.cache[key]=raw
            return raw
        except Exception:
            self.failed.add(key)
            raise


def publish(directory,report):
    backend=FilesystemBackend(Path(directory),writable=True)
    try:
        with (backend.root/'README.md').open('x',encoding='utf8') as stream:
            stream.write('Private frozen topic-evaluation reports. Not business acceptance or active calibration.\n')
    except FileExistsError:pass
    raw=json.dumps(report,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()
    if len(raw)>64*1024**2:raise EvaluationError('report_byte_budget')
    digest=sha256(raw).hexdigest();name='report-'+digest[:24]+'.json'
    path=backend._resolve(name,allow_missing=True)
    try:
        with path.open('xb') as stream:stream.write(raw)
    except FileExistsError:pass
    with backend.open_verified(name,digest,len(raw)):pass
    return path,digest
