"""Bounded authenticated publisher; no redirects, ambient proxy or credential logging."""
from functools import partial
from hashlib import sha256
import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict
import httpx

from media_contracts import Manifest, canonical


class PublishSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    token_file: str
    expected_active_release: str | None
    upload_transport: Literal["service", "oss"] = "service"


def push_bundle(bundle, settings, *, item=None, open_original=None):
    settings = PublishSettings.model_validate(settings)
    url = urlsplit(settings.base_url)
    if not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
        raise ValueError("explicit service origin required")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")):
        raise ValueError("HTTPS or local loopback required")
    token_path = Path(settings.token_file)
    if not token_path.is_absolute():
        raise ValueError("absolute private token file required")
    token = token_path.read_text(encoding="utf-8").strip()
    if not 1 <= len(token) <= 4096 or any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ValueError("invalid publisher credential")
    bundle = Path(bundle)
    raw = (bundle / "manifest.json").read_bytes()
    if len(raw) > 4 * 1024**2:
        raise ValueError("manifest byte budget")
    manifest = Manifest.model_validate_json(raw)
    # Portable bundles are self-contained. Queued items stream the verified local original.
    portable_objects = manifest.objects if item is None else ()
    for ref in portable_objects:
        path = bundle / "objects" / ref.sha256
        if path.is_symlink() or not path.is_file():
            raise ValueError("bundle object unavailable")
        actual, size = sha256(), 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                actual.update(chunk)
                size += len(chunk)
        if size != ref.size_bytes or actual.hexdigest() != ref.sha256:
            raise ValueError("bundle object changed")
    origin = settings.base_url.rstrip("/")
    with httpx.Client(base_url=origin, headers={"Authorization": "Bearer " + token}, timeout=120, follow_redirects=False, trust_env=False) as client:
        def request(method, path, **kwargs):
            try:
                with client.stream(method, path, **kwargs) as response:
                    raw = bytearray()
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 4 * 1024**2:
                            raise ValueError("publication response byte budget")
                    if not 200 <= response.status_code < 300:
                        raise ValueError("publication HTTP failure " + str(response.status_code))
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise ValueError("publication response invalid")
                    return result
            except httpx.HTTPError:
                raise ValueError("publication transport failed") from None
        # A first-time OSS integrity check can take several minutes for a multi-GiB video.
        index_timeout = httpx.Timeout(120, read=1200)
        prepared = request("POST", "/internal/releases", content=canonical(manifest),
                           headers={"Content-Type": "application/json"}, timeout=index_timeout)
        missing = prepared.get("missing_objects")
        refs = {ref.sha256: ref for ref in manifest.objects}
        if not isinstance(missing, list) or set(missing) - refs.keys():
            raise ValueError("publication response object scope invalid")
        uploaded = 0
        for object_sha in missing:
            if settings.upload_transport == "oss" or item is not None:
                from .oss import upload_object

                ticket = request("POST", f"/internal/releases/{manifest.release_id}/objects/{object_sha}/upload-url",
                                 timeout=index_timeout)
                reader = partial((bundle / "objects" / object_sha).open, "rb")
                if item is not None and object_sha == item["source_sha256"]:
                    reader = open_original
                uploaded += upload_object(ticket, refs[object_sha], reader)
                continue
            with (bundle / "objects" / object_sha).open("rb") as stream:
                def chunks():
                    while data := stream.read(1024 * 1024):
                        yield data
                request("PUT", f"/internal/releases/{manifest.release_id}/objects/{object_sha}", content=chunks(), headers={"Content-Type": "application/octet-stream"})
            uploaded += 1
        if item is not None:
            receipt = request("POST", f"/internal/releases/{manifest.release_id}/index-item", json=item, timeout=index_timeout)
            if (receipt.get("state") != "indexed" or receipt.get("release_id") != manifest.release_id
                    or any(receipt.get(key) != item[key] for key in ("asset_id", "source_sha256", "tag_version"))):
                raise ValueError("unexpected item receipt")
            return receipt
        verified = request("POST", f"/internal/releases/{manifest.release_id}/verify", timeout=index_timeout)
        active = request("POST", f"/internal/releases/{manifest.release_id}/activate",
                         json={"expected_active_release": settings.expected_active_release}, timeout=index_timeout)
    result = {"release_id": manifest.release_id, "uploaded_objects": uploaded, "verify": verified, "activation": active,
              "backend_verification": "independent_http_service"}
    (bundle / "cloud-receipt.json").write_bytes(canonical(result))
    return result
