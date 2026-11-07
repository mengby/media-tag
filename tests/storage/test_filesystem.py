from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path

import pytest

from media_tag.storage import BackendNotConfigured, IntegrityError, StorageError, UnsafePath
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.storage.oss import OssBackend


def test_copy_is_immutable_deduplicated_and_not_a_hard_link_to_original(tmp_path):
    original = tmp_path / "synthetic-original.bin"
    original.write_bytes(b"synthetic original\x00\xff")
    objects = tmp_path / "objects-root"
    objects.mkdir()
    store = FilesystemBackend(objects, writable=True)
    before = original.read_bytes()
    with original.open("rb") as stream:
        blob = store.ingest(stream)
    with original.open("rb") as stream:
        assert store.ingest(stream) == blob
    with store.open_verified(blob.object_key, blob.sha256, blob.size_bytes) as stream:
        assert stream.read() == before
    assert original.read_bytes() == before
    assert not os.path.samefile(original, objects / blob.object_key)
    assert len(list(objects.rglob(blob.sha256))) == 1
    assert not list(objects.glob(".ingest-*"))


def test_empty_blob_and_read_only_backend(tmp_path):
    readonly = FilesystemBackend(tmp_path)
    with pytest.raises(StorageError, match="read-only"):
        readonly.ingest(BytesIO(b"synthetic"))
    store = FilesystemBackend(tmp_path, writable=True)
    blob = store.ingest(BytesIO(b""))
    assert blob.sha256 == sha256(b"").hexdigest() and blob.size_bytes == 0


@pytest.mark.parametrize('outside', [False, True])
def test_windows_extended_path_alias_keeps_real_containment_checks(tmp_path, monkeypatch, outside):
    root = tmp_path / 'objects'; root.mkdir()
    store = FilesystemBackend(root, writable=True)
    blob = store.ingest(BytesIO(b'synthetic extended-prefix race'))
    target = root / blob.object_key
    external = tmp_path / 'outside.bin'; external.write_bytes(b'synthetic outside')
    original = type(target).resolve
    def extended(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if os.name == 'nt' and path == target:
            return type(path)('\\\\?\\' + str(external if outside else result))
        return result
    monkeypatch.setattr(type(target), 'resolve', extended)
    if outside and os.name == 'nt':
        with pytest.raises(UnsafePath):
            with store.open_verified(blob.object_key, blob.sha256, blob.size_bytes):
                pytest.fail('an outside object must not be opened')
    else:
        with store.open_verified(blob.object_key, blob.sha256, blob.size_bytes) as stream:
            assert stream.read() == b'synthetic extended-prefix race'
    assert external.read_bytes() == b'synthetic outside'


@pytest.mark.parametrize("key", ["../secret", "/secret", "C:/secret", "a/../secret", "a//b", "a\\b", "a/CON.txt", "a/thing.", "https://example.test/file"])
def test_unsafe_keys_rejected(tmp_path, key):
    store = FilesystemBackend(tmp_path)
    with pytest.raises(UnsafePath):
        with store.open_read(key):
            pytest.fail("unsafe file opened")


def test_actual_link_or_junction_is_rejected(tmp_path):
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "synthetic.bin").write_bytes(b"synthetic outside")
    link = root / "link"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        with pytest.raises(UnsafePath):
            with FilesystemBackend(root).open_read("link/synthetic.bin"):
                pytest.fail("linked file opened")
    finally:
        if os.name == "nt":
            os.rmdir(link)
        else:
            link.unlink()
    assert (outside / "synthetic.bin").read_bytes() == b"synthetic outside"


def test_corrupt_or_missing_object_never_overwritten(tmp_path):
    store = FilesystemBackend(tmp_path, writable=True)
    blob = store.ingest(BytesIO(b"synthetic content"))
    path = tmp_path / blob.object_key
    path.write_bytes(b"corrupt")
    with pytest.raises(IntegrityError):
        store.ingest(BytesIO(b"synthetic content"))
    assert path.read_bytes() == b"corrupt"
    path.unlink()
    with pytest.raises(StorageError):
        with store.open_verified(blob.object_key, blob.sha256, blob.size_bytes):
            pytest.fail("missing object opened")


def test_stream_failure_leaves_no_published_or_temporary_file(tmp_path):
    class BrokenStream(BytesIO):
        def read(self, size=-1):
            raise OSError("synthetic read failure")
    with pytest.raises(StorageError):
        FilesystemBackend(tmp_path, writable=True).ingest(BrokenStream())
    assert list(tmp_path.iterdir()) == []


def test_actual_open_handle_is_checked_even_if_name_check_passed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    allowed = root / "synthetic.bin"
    outside = tmp_path / "outside.bin"
    allowed.write_bytes(b"synthetic inside")
    outside.write_bytes(b"synthetic outside")
    original_open = Path.open
    def redirected_open(path, *args, **kwargs):
        return original_open(outside if path == allowed else path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", redirected_open)
    with pytest.raises(UnsafePath):
        with FilesystemBackend(root).open_read("synthetic.bin"):
            pytest.fail("redirected external handle returned")


def test_oss_stub_always_fails_without_fallback():
    backend = OssBackend()
    with pytest.raises(BackendNotConfigured):
        backend.ingest(BytesIO(b"synthetic"))
    with pytest.raises(BackendNotConfigured):
        backend.open_verified("synthetic", "a" * 64, 1)
