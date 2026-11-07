"""Explicitly unavailable until D08 supplies and validates a real OSS adapter."""

from . import BackendNotConfigured


class OssBackend:
    def ingest(self, source):
        raise BackendNotConfigured("OSS backend is not configured")

    def open_verified(self, key, sha256, size_bytes):
        raise BackendNotConfigured("OSS backend is not configured")
