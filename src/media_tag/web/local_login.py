"""A private OS-user handoff, never an anonymous loopback identity."""
from hashlib import sha256
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlsplit
from pydantic import Field,model_validator
from media_tag.contracts.models import ContractModel,Sha256
from media_tag.deployment.local import directory,lock,write_json
from media_tag.storage.filesystem import FilesystemBackend


TICKET_SECONDS=60


class LocalBrowserLogin(ContractModel):
    origin: str
    directory: str

    @model_validator(mode='after')
    def local_only(self):
        url=urlsplit(self.origin)
        if url.scheme!='http' or url.hostname!='127.0.0.1' or url.username or url.password or url.path or url.query or url.fragment or url.port is None or not 1<=url.port<=65535:
            raise ValueError('explicit IPv4 loopback origin and port required')
        if not Path(self.directory).is_absolute():raise ValueError('absolute private login directory required')
        return self


class TicketRecord(ContractModel):
    credential_sha256: Sha256
    issued_at: float
    expires_at: float


class TicketInput(ContractModel):
    ticket: str=Field(pattern=r'^[A-Za-z0-9_-]{43}$')


class TicketStore:
    def __init__(self,config):
        self.config=config;self.root=directory(config.directory)
        self.started_at=time.time()
        self.backend=FilesystemBackend(self.root,writable=True)

    def record(self,path):
        with self.backend.open_read(path.name) as stream:raw=stream.read(2049)
        if len(raw)>2048:raise ValueError('invalid local login record')
        return TicketRecord.model_validate_json(raw)

    def issue(self,credential_sha256):
        now=time.time()
        with lock(self.root,name='tickets.lock'):
            active=0
            for path in self.root.glob('*.json'):
                if re.fullmatch(r'[0-9a-f]{64}\.json',path.name) is None:continue
                record=self.record(path)
                if record.expires_at<=now:self.backend._resolve(path.name).unlink()
                else:active+=1
            if active>=32:raise ValueError('local login ticket capacity reached')
            ticket=secrets.token_urlsafe(32)
            record=TicketRecord(credential_sha256=credential_sha256,issued_at=now,expires_at=now+TICKET_SECONDS)
            write_json(self.root,sha256(ticket.encode()).hexdigest()+'.json',record.model_dump(mode='json'))
        return ticket

    def consume(self,ticket,credentials):
        if re.fullmatch(r'[A-Za-z0-9_-]{43}',ticket) is None:raise ValueError('invalid local login ticket')
        with lock(self.root,name='tickets.lock'):
            path=self.backend._resolve(sha256(ticket.encode()).hexdigest()+'.json')
            record=self.record(path);path.unlink()
        now=time.time()
        if not self.started_at<=record.issued_at<=now<record.expires_at<=record.issued_at+TICKET_SECONDS:raise ValueError('expired local login ticket')
        principal=credentials.get(record.credential_sha256)
        if principal is None:raise ValueError('local login credential unavailable')
        return principal

    def allows(self,request):
        return bool(request.client and request.client.host=='127.0.0.1'
            and str(request.base_url).rstrip('/')==self.config.origin
            and request.headers.get('origin')==self.config.origin
            and request.headers.get('sec-fetch-site') in (None,'same-origin')
            and not any(k in request.headers for k in ('forwarded','x-forwarded-for','x-forwarded-host','x-forwarded-proto')))
