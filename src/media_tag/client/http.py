"""Read-only HTTP facade. No ambient proxies, redirects, arbitrary paths or fallback origins."""
from pathlib import Path
import json
import re
import threading
import time
from typing import Annotated
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit
from urllib.request import Request,ProxyHandler,HTTPRedirectHandler,build_opener
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel
from media_tag.catalog.service import encode


class LibraryClientError(Exception):
    def __init__(self,code,*,status=None,retryable=False):
        self.code,self.status,self.retryable=code,status,retryable
        super().__init__(code)

    def payload(self):return {'error':{'code':self.code,'status':self.status,'retryable':self.retryable}}


class ClientSettings(ContractModel):
    base_url: str
    token_file: str
    timeout_seconds: Annotated[float,Field(ge=1,le=30)]=15.0
    response_limit_bytes: Annotated[int,Field(strict=True,ge=1024,le=16*1024**2)]=4*1024**2

    @model_validator(mode='after')
    def explicit_origin(self):
        origin=urlsplit(self.base_url)
        if not origin.hostname or origin.username or origin.password or origin.query or origin.fragment or origin.path not in ('','/'):
            raise ValueError('explicit service origin required')
        if origin.scheme!='https' and not (origin.scheme=='http' and origin.hostname in ('localhost','127.0.0.1','::1')):
            raise ValueError('HTTPS or local loopback required')
        if not Path(self.token_file).is_absolute():raise ValueError('absolute private token file required')
        return self


def load_settings(path):
    with Path(path).open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise LibraryClientError('client_config_byte_budget')
    return ClientSettings.model_validate_json(raw)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise LibraryClientError('upstream_redirect_refused')


class LibraryClient:
    def __init__(self,settings):
        self.settings=settings;self.origin=settings.base_url.rstrip('/')
        with Path(settings.token_file).open('r',encoding='utf8') as stream:raw_token=stream.read(4097)
        token=raw_token.strip()
        if len(raw_token)>4096 or not token or any(c.isspace() or ord(c)<33 or ord(c)>126 for c in token):raise LibraryClientError('invalid_client_credential')
        self._token=token;self._slots=threading.BoundedSemaphore(4)
        self._opener=build_opener(ProxyHandler({}),NoRedirect())

    def _request(self,method,path,payload=None):
        parsed=urlsplit(path)
        if method not in ('GET','POST') or method=='POST' and path!='/v1/assets/search' or parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith('/v1/') or any(p in ('.','..') for p in parsed.path.split('/')):
            raise LibraryClientError('untrusted_api_path')
        body=encode(payload).encode() if payload is not None else None
        if body is not None and len(body)>128*1024:raise LibraryClientError('tool_request_byte_budget')
        if not self._slots.acquire(blocking=False):raise LibraryClientError('client_busy',retryable=True)
        started=time.monotonic()
        try:
            request=Request(self.origin+path,data=body,method=method,headers={'Authorization':'Bearer '+self._token,'Content-Type':'application/json','Cache-Control':'no-store'})
            try:
                with self._opener.open(request,timeout=self.settings.timeout_seconds) as response:
                    raw=response.read(self.settings.response_limit_bytes+1)
            except HTTPError as exc:
                # Only carry bounded code/status, never the upstream body or URL.
                try:
                    value=json.loads(exc.read(8193));code=value.get('error',{}).get('code','http_'+str(exc.code))
                    retryable=value.get('error',{}).get('retryable',False) is True
                except Exception:code='http_'+str(exc.code);retryable=exc.code in (429,502,503,504)
                finally:exc.close()
                if not isinstance(code,str) or re.fullmatch(r'[a-z][a-z0-9_]{0,79}',code) is None:code='http_'+str(exc.code)
                raise LibraryClientError(code,status=exc.code,retryable=retryable) from None
            except TimeoutError:raise LibraryClientError('upstream_timeout',retryable=True) from None
            except URLError as exc:raise LibraryClientError('upstream_timeout' if isinstance(exc.reason,TimeoutError) else 'upstream_unavailable',retryable=True) from None
            if time.monotonic()-started>self.settings.timeout_seconds:raise LibraryClientError('upstream_timeout',retryable=True)
            if len(raw)>self.settings.response_limit_bytes:raise LibraryClientError('upstream_response_byte_budget')
            try:value=json.loads(raw)
            except (ValueError,UnicodeError):raise LibraryClientError('upstream_invalid_response') from None
            if not isinstance(value,dict):raise LibraryClientError('upstream_invalid_response')
            return value
        finally:self._slots.release()

    def require_reader(self,*,search=False):
        capabilities=self._request('GET','/v1/capabilities')
        if capabilities.get('schema_version')!='d17-api-v1' or capabilities.get('read_only_identity') is not True:
            raise LibraryClientError('reader_credential_required')
        if capabilities.get('fixed_version_files') is not True or search and capabilities.get('search_enabled') is not True:
            raise LibraryClientError('required_api_capability_unavailable')
        return capabilities

    def search(self,spec):
        self.require_reader(search=True)
        value=self._request('POST','/v1/assets/search',spec.model_dump(mode='json'))
        if value.get('schema_version')!='d17-search-v1':raise LibraryClientError('upstream_schema_mismatch')
        return value

    def asset(self,asset_id):
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',asset_id) is None:raise LibraryClientError('invalid_asset_id')
        self.require_reader();value=self._request('GET','/v1/assets/'+asset_id)
        if value.get('asset_id')!=asset_id or value.get('schema_version')!='d17-files-v1':raise LibraryClientError('upstream_schema_mismatch')
        return value

    def manifest(self,asset_id,digest,*,preview=False,start_seconds=None,end_seconds=None):
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',asset_id) is None or re.fullmatch(r'[0-9a-f]{64}',digest) is None:raise LibraryClientError('invalid_fixed_version')
        if start_seconds is not None or end_seconds is not None:
            from media_tag.files.models import PlaybackInterval
            from pydantic import ValidationError
            try:PlaybackInterval(start_seconds=start_seconds,end_seconds=end_seconds)
            except ValidationError:raise LibraryClientError('invalid_playback_interval') from None
            if not preview:raise LibraryClientError('playback_requires_preview_manifest')
        self.require_reader();path=f"/v1/assets/{asset_id}/{'preview-manifest' if preview else 'download-manifest'}?sha256={digest}"
        if start_seconds is not None or end_seconds is not None:path+=f'&start_seconds={start_seconds}&end_seconds={end_seconds}'
        value=self._request('GET',path)
        if value.get('schema_version')!='d17-files-v1' or value.get('asset_id')!=asset_id or value.get('source_sha256' if preview else 'sha256')!=digest:
            raise LibraryClientError('upstream_fixed_version_mismatch')
        return value
