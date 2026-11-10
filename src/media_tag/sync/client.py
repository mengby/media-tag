"""Bounded cloud service transport. No redirects, ambient proxy or URL credentials."""
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

import httpx
from media_tag.client.http import ClientSettings


class SyncError(Exception):
    def __init__(self,code,status=None):self.code,self.status=code,status;super().__init__(code)


class CloudClient:
    def __init__(self,settings):
        self.settings=ClientSettings.model_validate_json(settings.model_dump_json())
        with Path(settings.token_file).open('r',encoding='utf-8') as f:token=f.read(4097).strip()
        if not 1<=len(token)<=4096 or any(ord(c)<33 or ord(c)>126 for c in token):raise SyncError('sync_credential_invalid')
        self.client=httpx.Client(base_url=settings.base_url.rstrip('/'),headers={'Authorization':'Bearer '+token},trust_env=False,follow_redirects=False,timeout=settings.timeout_seconds)

    def close(self):self.client.close()

    def json(self,method,path,*,payload=None,missing_ok=False):
        if not path.startswith('/internal/sync/') or '..' in path.split('?')[0].split('/'):
            raise SyncError('untrusted_sync_path')
        try:
            with self.client.stream(method,path,json=payload) as response:
                if missing_ok and response.status_code==404:return None
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>4*1024**2:raise SyncError('sync_response_budget')
                if not 200<=response.status_code<300:raise SyncError('sync_http_'+str(response.status_code),response.status_code)
                value=json.loads(raw)
                if not isinstance(value,dict):raise SyncError('sync_response_invalid')
                return value
        except httpx.HTTPError:raise SyncError('sync_transport_unavailable') from None

    def download(self,event,destination):
        destination=Path(destination);temporary=None
        try:
            with self.client.stream('GET',f'/internal/sync/uploads/{event.upload_id}/content') as response:
                if response.status_code!=200:raise SyncError('sync_download_http_'+str(response.status_code),response.status_code)
                with tempfile.NamedTemporaryFile(dir=destination.parent,delete=False) as output:
                    temporary=Path(output.name);actual=sha256();size=0
                    for chunk in response.iter_bytes(chunk_size=1024**2):
                        size+=len(chunk)
                        if size>event.blob.size_bytes:raise SyncError('sync_download_budget')
                        actual.update(chunk);output.write(chunk)
                    output.flush();os.fsync(output.fileno())
                if size!=event.blob.size_bytes or actual.hexdigest()!=event.blob.sha256:raise SyncError('sync_download_integrity')
                # A cached file is always re-authorized and revalidated, never trusted by name.
                if destination.exists():
                    if destination.is_symlink() or not destination.is_file():raise SyncError('sync_cached_content_changed')
                    cached=sha256();cached_size=0
                    with destination.open('rb') as stream:
                        while chunk:=stream.read(1024**2):cached.update(chunk);cached_size+=len(chunk)
                    if cached.hexdigest()!=event.blob.sha256 or cached_size!=event.blob.size_bytes:raise SyncError('sync_cached_content_changed')
                else:os.link(temporary,destination)
                return destination
        except httpx.HTTPError:raise SyncError('sync_download_transport') from None
        finally:
            if temporary:temporary.unlink(missing_ok=True)
