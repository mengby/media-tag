"""Bounded authenticated publisher; no redirects, ambient proxy or credential logging."""
from hashlib import sha256
import json
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import BaseModel, ConfigDict
import httpx

from media_contracts import Manifest, canonical


class PublishSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    token_file: str
    expected_active_release: str | None


def push_bundle(bundle, settings):
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
    # Verify the full bundle before any cloud mutation, including resumed pushes.
    for ref in manifest.objects:
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
        prepared = request("POST", "/internal/releases", content=canonical(manifest), headers={"Content-Type": "application/json"})
        missing = prepared.get("missing_objects")
        if not isinstance(missing, list) or set(missing) - {b.sha256 for b in manifest.objects}:
            raise ValueError("publication response object scope invalid")
        uploaded = 0
        for object_sha in missing:
            with (bundle / "objects" / object_sha).open("rb") as stream:
                def chunks():
                    while data := stream.read(1024 * 1024):
                        yield data
                request("PUT", f"/internal/releases/{manifest.release_id}/objects/{object_sha}", content=chunks(), headers={"Content-Type": "application/octet-stream"})
            uploaded += 1
        verified = request("POST", f"/internal/releases/{manifest.release_id}/verify")
        active = request("POST", f"/internal/releases/{manifest.release_id}/activate", json={"expected_active_release": settings.expected_active_release})
    result = {"release_id": manifest.release_id, "uploaded_objects": uploaded, "verify": verified, "activation": active,
              "backend_verification": "independent_http_service"}
    (bundle / "cloud-receipt.json").write_bytes(canonical(result))
    return result
