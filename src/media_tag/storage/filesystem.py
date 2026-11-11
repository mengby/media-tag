"""Read-only sources and copy-on-ingest, content-addressed local objects."""

from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import stat
import tempfile
from typing import BinaryIO, Iterator

from media_tag.contracts.models import validate_relative_path
from . import IntegrityError, StorageError, StoredBlob, UnsafePath

CHUNK_SIZE = 1024 * 1024


def _canonical(path: Path) -> Path:
    resolved = Path(path).resolve()
    # During concurrent creation Windows realpath can retain its extended prefix.
    # These are equivalent spellings, not different roots. Keep reparse and handle checks below.
    if os.name == 'nt':
        value = str(resolved)
        if value.startswith('\\\\?\\UNC\\'):
            return Path('\\\\' + value[8:])
        if value.startswith('\\\\?\\') and len(value) >= 7 and value[4].isalpha() and value[5:7] == ':\\':
            return Path(value[4:])
    return resolved


def require_local_path(path: Path) -> None:
    if str(path).startswith(("\\\\", "//")):
        raise UnsafePath("a local filesystem path is required")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(path.resolve().anchor)) == 4:
            raise UnsafePath("network-mapped paths are not supported for local state")


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _opened_path(stream: BinaryIO, candidate: Path) -> Path:
    """Validate the actual Windows handle, not only a pathname before open."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
        function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        function.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = function(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if length == 0 or length >= len(buffer):
            raise UnsafePath("cannot resolve the opened file handle")
        value = buffer.value
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
        return _canonical(Path(value))
    handle = Path(f"/proc/self/fd/{stream.fileno()}")
    if handle.exists():
        return _canonical(handle)
    if not os.path.samestat(os.fstat(stream.fileno()), candidate.stat()):
        raise UnsafePath("file identity changed while opening")
    return _canonical(candidate)


class FilesystemBackend:
    def __init__(self, root: Path, *, writable: bool = False) -> None:
        root = Path(root)
        if not root.is_dir() or _linked(root):
            raise UnsafePath("backend root must be an existing, non-linked directory")
        self.root = _canonical(root)
        self.writable = writable
        if writable:
            require_local_path(self.root)

    def _resolve(self, key: str, *, allow_missing: bool = False) -> Path:
        try:
            validate_relative_path(key)
        except ValueError as exc:
            raise UnsafePath("invalid relative storage key") from exc
        candidate = self.root
        for part in key.split("/"):
            candidate = candidate / part
            if candidate.exists() or candidate.is_symlink():
                if _linked(candidate):
                    raise UnsafePath("linked/reparse path components are not allowed")
        resolved = _canonical(candidate)
        if not resolved.is_relative_to(self.root):
            raise UnsafePath("storage key escapes backend root")
        if not allow_missing and not resolved.is_file():
            raise StorageError("stored file is missing or not a regular file")
        return resolved

    @contextmanager
    def open_read(self, key: str) -> Iterator[BinaryIO]:
        path = self._resolve(key)
        try:
            with path.open("rb") as stream:
                actual = _opened_path(stream, path)
                if not actual.is_relative_to(self.root) or not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise UnsafePath("opened object is outside the backend or not a regular file")
                yield stream
        except OSError as exc:
            raise StorageError("cannot read stored file") from exc

    @contextmanager
    def open_verified(self, key: str, expected_sha256: str, size_bytes: int) -> Iterator[BinaryIO]:
        with self.open_read(key) as stream:
            expected_size = os.fstat(stream.fileno()).st_size
            if expected_size != size_bytes:
                raise IntegrityError("stored content does not match its registered hash/size")
            digest, size = sha256(), 0
            while chunk := stream.read(min(CHUNK_SIZE, expected_size - size + 1)):
                size += len(chunk)
                if size > expected_size:
                    raise IntegrityError("stored content does not match its registered hash/size")
                digest.update(chunk)
            if size != expected_size or os.fstat(stream.fileno()).st_size != expected_size or digest.hexdigest() != expected_sha256:
                raise IntegrityError("stored content does not match its registered hash/size")
            stream.seek(0)
            yield stream

    def ingest(self, source: BinaryIO) -> StoredBlob:
        if not self.writable:
            raise StorageError("backend is read-only")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".ingest-", dir=self.root, delete=False) as output:
                temporary = Path(output.name)
                digest, size = sha256(), 0
                while chunk := source.read(CHUNK_SIZE):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                output.flush()
                os.fsync(output.fileno())
            key = f"objects/{digest.hexdigest()[:2]}/{digest.hexdigest()}"
            destination = self._resolve(key, allow_missing=True)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # Recheck newly created parents before publishing; never overwrite an existing object.
            destination = self._resolve(key, allow_missing=True)
            try:
                os.link(temporary, destination)
            except FileExistsError:
                pass
            with self.open_verified(key, digest.hexdigest(), size):
                pass
            return StoredBlob(digest.hexdigest(), size, key)
        except OSError as exc:
            raise StorageError("immutable object copy/publication failed") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
