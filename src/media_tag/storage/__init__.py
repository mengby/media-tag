"""Backend-neutral immutable object interface."""

from dataclasses import dataclass
from typing import BinaryIO, ContextManager, Protocol


class StorageError(Exception):
    pass


class UnsafePath(StorageError):
    pass


class IntegrityError(StorageError):
    pass


class BackendNotConfigured(StorageError):
    pass


@dataclass(frozen=True)
class StoredBlob:
    sha256: str
    size_bytes: int
    object_key: str


class StorageBackend(Protocol):
    def ingest(self, source: BinaryIO) -> StoredBlob: ...

    def open_verified(self, key: str, sha256: str, size_bytes: int) -> ContextManager[BinaryIO]: ...
