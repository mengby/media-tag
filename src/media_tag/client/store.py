"""An explicitly owned new job; fixed generated keys, checked handles and immutable JSON."""
from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from uuid import uuid4
from pydantic import TypeAdapter
from media_tag.catalog.service import encode
from media_tag.contracts.models import Identifier,validate_relative_path
from media_tag.storage.filesystem import FilesystemBackend,_linked,_opened_path,require_local_path
from .http import LibraryClientError


class TaskStore:
    def __init__(self,jobs_root,job_id):
        root=Path(jobs_root)
        TypeAdapter(Identifier).validate_python(job_id,strict=True)
        validate_relative_path(job_id)
        if not root.is_absolute() or root.name!='jobs' or not root.is_dir():raise LibraryClientError('explicit_jobs_root_required')
        require_local_path(root)
        for path in (root,*root.parents):
            if _linked(path):raise LibraryClientError('linked_task_path')
        self.jobs=FilesystemBackend(root,writable=True)
        task=self.jobs._resolve(job_id,allow_missing=True)
        marker={'schema_version':'d19-task-v1','provider':'media-tag','job_id':job_id}
        try:
            task.mkdir()
        except FileExistsError:
            if not task.is_dir():raise LibraryClientError('invalid_task_directory') from None
            try:
                with self.jobs.open_read(job_id+'/media-tag-task.json') as stream:value=json.loads(stream.read(4097))
            except Exception:raise LibraryClientError('existing_unmanaged_task_refused') from None
            if value!=marker:raise LibraryClientError('existing_unmanaged_task_refused')
        else:
            with (task/'media-tag-task.json').open('x',encoding='utf8') as stream:stream.write(encode(marker))
            (task/'README.md').write_text('D19 task-local media-tag acquisition. Originals/shared assets are outside this task. No medical approval.\n',encoding='utf8')
        self.task=task;self.job_id=job_id;self.backend=FilesystemBackend(task,writable=True)
        for key in ('edit','edit/media-tag'):
            path=self.backend._resolve(key,allow_missing=True);path.mkdir(exist_ok=True)
            self.backend._resolve(key,allow_missing=True)
            readme=path/'README.md'
            if not readme.exists():readme.write_text('Task-local fixed media, partial downloads, immutable provenance receipts and preview handoffs. Checks do not grant final export.\n',encoding='utf8')

    def path(self,name):return self.backend._resolve('edit/media-tag/'+name,allow_missing=True)

    @contextmanager
    def open(self,name,mode):
        path=self.path(name)
        # Never truncate an unchecked path; validate its actual handle before writing.
        with path.open(mode) as stream:
            if _opened_path(stream,path)!=path or not stat.S_ISREG(os.fstat(stream.fileno()).st_mode) or os.fstat(stream.fileno()).st_nlink!=1:
                raise LibraryClientError('unsafe_task_handle')
            yield stream

    @contextmanager
    def lock(self,key):
        name=key+'.lock'
        try:
            with self.open(name,'xb') as stream:stream.write(b'0')
        except FileExistsError:pass
        with self.open(name,'r+b') as stream:
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise LibraryClientError('task_download_busy',retryable=True) from None
            try:yield
            finally:
                stream.seek(0)
                if os.name=='nt':msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:fcntl.flock(stream,fcntl.LOCK_UN)

    def verify(self,name,digest,size=None):
        actual=sha256();length=0
        with self.open(name,'rb') as stream:
            while chunk:=stream.read(1024**2):actual.update(chunk);length+=len(chunk)
        if actual.hexdigest()!=digest or size is not None and length!=size:raise LibraryClientError('task_file_integrity_failed')
        return length

    def write_json(self,kind,value):
        raw=encode(value).encode();digest=sha256(raw).hexdigest();name=kind+'-'+digest+'.json'
        if len(raw)>4*1024**2:raise LibraryClientError('task_record_byte_budget')
        if self.path(name).exists():self.verify(name,digest,len(raw));return digest
        temporary='write-'+uuid4().hex+'.tmp'
        try:
            with self.open(temporary,'xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
            try:os.link(self.path(temporary),self.path(name))
            except FileExistsError:pass
        finally:self.path(temporary).unlink(missing_ok=True)
        self.verify(name,digest,len(raw));return digest

    def read_json(self,kind,digest):
        from media_tag.contracts.models import Sha256 as SHA256
        TypeAdapter(SHA256).validate_python(digest,strict=True)
        name=kind+'-'+digest+'.json'
        if self.path(name).stat().st_size>4*1024**2:raise LibraryClientError('task_record_byte_budget')
        self.verify(name,digest)
        with self.open(name,'rb') as stream:raw=stream.read(4*1024**2+1)
        if len(raw)>4*1024**2:raise LibraryClientError('task_record_byte_budget')
        return json.loads(raw)
