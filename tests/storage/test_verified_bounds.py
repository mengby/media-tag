"""Real filesystem corruption races cannot force unbounded verification reads."""
from contextlib import contextmanager
from io import BytesIO

import pytest

from media_tag.storage import IntegrityError
from media_tag.storage.filesystem import CHUNK_SIZE,FilesystemBackend


def stored(tmp_path,data):
    (tmp_path/'README.md').write_text('Synthetic private bounded-storage verification fixtures.\n')
    backend=FilesystemBackend(tmp_path,writable=True)
    blob=backend.ingest(BytesIO(data))
    return backend,blob,tmp_path/blob.object_key


def tracked(monkeypatch,backend,*,before=None,after=None):
    actual=backend.open_read;reads=[]
    @contextmanager
    def opened(key):
        with actual(key) as stream:
            class Counted:
                def __getattr__(self,name):return getattr(stream,name)
                def read(self,size=-1):
                    assert size>=0  # No implicit read-to-EOF is permitted.
                    if before:before(len(reads),size)
                    data=stream.read(size);reads.append(len(data))
                    if after:after(len(reads)-1,data)
                    return data
            yield Counted()
    monkeypatch.setattr(backend,'open_read',opened)
    return reads


def test_already_inflated_object_is_rejected_without_reading_content(tmp_path,monkeypatch):
    backend,blob,path=stored(tmp_path,b'synthetic original')
    with path.open('ab') as stream:stream.write(b'x'*(8*1024**2))
    reads=tracked(monkeypatch,backend)
    with pytest.raises(IntegrityError):
        with backend.open_verified(blob.object_key,blob.sha256,blob.size_bytes):pytest.fail('corrupt bytes yielded')
    assert reads==[]


def test_real_growth_while_reading_is_capped_at_declared_size_plus_one(tmp_path,monkeypatch):
    data=b'a'*(CHUNK_SIZE+17);backend,blob,path=stored(tmp_path,data)
    def grow(index,chunk):
        if index==0:
            with path.open('ab') as stream:stream.write(b'growth'*(1024**2))
    reads=tracked(monkeypatch,backend,after=grow)
    with pytest.raises(IntegrityError):
        with backend.open_verified(blob.object_key,blob.sha256,blob.size_bytes):pytest.fail('growing bytes yielded')
    assert sum(reads)==blob.size_bytes+1
    assert path.stat().st_size>sum(reads)


def test_real_growth_after_eof_is_rejected_by_final_handle_size(tmp_path,monkeypatch):
    backend,blob,path=stored(tmp_path,b'a'*4096)
    def grow_at_eof(index,chunk):
        if not chunk:
            with path.open('ab') as stream:stream.write(b'late growth')
    reads=tracked(monkeypatch,backend,after=grow_at_eof)
    with pytest.raises(IntegrityError):
        with backend.open_verified(blob.object_key,blob.sha256,blob.size_bytes):pytest.fail('late growth yielded')
    assert sum(reads)==blob.size_bytes


def test_real_truncation_while_reading_never_yields_short_content(tmp_path,monkeypatch):
    backend,blob,path=stored(tmp_path,b'a'*(CHUNK_SIZE+17))
    def truncate(index,size):
        if index==0:
            with path.open('r+b') as stream:stream.truncate(blob.size_bytes//2)
    reads=tracked(monkeypatch,backend,before=truncate)
    with pytest.raises(IntegrityError):
        with backend.open_verified(blob.object_key,blob.sha256,blob.size_bytes):pytest.fail('short bytes yielded')
    assert sum(reads)<blob.size_bytes


def test_same_size_corruption_still_requires_exact_sha256(tmp_path):
    backend,blob,path=stored(tmp_path,b'abcdef')
    path.write_bytes(b'fedcba')
    with pytest.raises(IntegrityError):
        with backend.open_verified(blob.object_key,blob.sha256,blob.size_bytes):pytest.fail('wrong hash yielded')


@pytest.mark.parametrize('data',[b'',b'valid',(b'x'*(CHUNK_SIZE+17))])
def test_valid_objects_still_yield_verified_stream_at_offset_zero(tmp_path,data):
    backend,blob,_=stored(tmp_path,data)
    with backend.open_verified(blob.object_key,blob.sha256,blob.size_bytes) as stream:
        assert stream.tell()==0 and stream.read()==data
