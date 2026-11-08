"""Editor-only intake transport, separate from the read-only MCP client."""
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from urllib.error import HTTPError,URLError
from urllib.request import Request,ProxyHandler,build_opener
from media_tag.catalog.service import encode
from media_tag.client.http import ClientSettings,NoRedirect
from .models import IntakeError


class IntakeClient:
    def __init__(self,settings):
        self.settings=settings;self.origin=settings.base_url.rstrip('/')
        with Path(settings.token_file).open('r',encoding='utf8') as stream:raw=stream.read(4097)
        token=raw.strip()
        if len(raw)>4096 or not token or any(ord(c)<33 or ord(c)>126 for c in token):raise IntakeError('invalid_intake_credential')
        self._token=token;self.opener=build_opener(ProxyHandler({}),NoRedirect());self.retry_count=0

    def call(self,method,path,*,payload=None,content=None,headers=None):
        allowed=(method=='GET' and re.fullmatch(r'/(ui/session|ui/library/asset-[a-f0-9]+|uploads/upload-[a-f0-9]+|v1/assets/asset-[a-f0-9]+|jobs/job-[a-f0-9]+)',path)) or (
            method=='POST' and re.fullmatch(r'/uploads(?:/upload-[a-f0-9]+/complete)?',path)) or (
            method=='PUT' and re.fullmatch(r'/uploads/upload-[a-f0-9]+/chunks\?offset=[0-9]+',path))
        if not allowed or payload is not None and content is not None:raise IntakeError('untrusted_intake_path')
        data=encode(payload).encode() if payload is not None else content
        if data is not None and len(data)>8*1024**2:raise IntakeError('intake_request_byte_budget')
        for attempt in range(3):
            try:
                request=Request(self.origin+path,data=data,method=method,headers={
                    'Authorization':'Bearer '+self._token,'Content-Type':'application/json' if payload is not None else 'application/octet-stream',
                    'Cache-Control':'no-store',**(headers or {})})
                with self.opener.open(request,timeout=self.settings.timeout_seconds) as response:
                    raw=response.read(self.settings.response_limit_bytes+1)
                if len(raw)>self.settings.response_limit_bytes:raise IntakeError('intake_response_byte_budget')
                try:value=json.loads(raw)
                except (ValueError,UnicodeError):raise IntakeError('intake_invalid_response') from None
                if not isinstance(value,dict):raise IntakeError('intake_invalid_response')
                return value
            except HTTPError as exc:
                status=exc.code;exc.close();error=IntakeError('intake_http_'+str(status),retryable=status in (429,502,503,504),status=status)
            except (URLError,TimeoutError,OSError):error=IntakeError('intake_transport_unavailable',retryable=True)
            if not error.retryable or attempt==2:raise error from None
            self.retry_count+=1;time.sleep(.2*(2**attempt))

    def editor(self):
        person=self.call('GET','/ui/session')
        if person.get('role') not in ('editor','admin') or not person.get('features',{}).get('upload'):
            raise IntakeError('intake_editor_required')
        return person['actor_id']


def local_settings(root):
    from media_tag.deployment.local import load
    root,instance=load(root)
    return ClientSettings(base_url=f'http://127.0.0.1:{instance.port}',token_file=str(root/'owner-token.local.txt'),timeout_seconds=30.0)
