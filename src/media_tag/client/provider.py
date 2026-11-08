"""Explicit media-tag provider, fixed bytes only, task-local resume and preview handoff."""
from hashlib import sha256
import os
import time
from http.client import HTTPException
from urllib.error import HTTPError,URLError
from urllib.request import Request
from media_tag.catalog.models import SourceInput
from media_tag.catalog.service import encode
from media_tag.files.service import media_type
from media_tag.workers.probe import ProbeOptions,probe_media,FORMAT_WHITELIST
from .http import LibraryClientError
from .models import Acquisition,CurrentUseCheck
from .store import TaskStore

SUFFIX={'image/png':'png','image/jpeg':'jpg','image/webp':'webp','image/gif':'gif','video/mp4':'mp4'}


class TaskProvider:
    def __init__(self,client,jobs_root,job_id,tools,*,max_bytes=512*1024**2,transfer_seconds=300.0,license_validation=True):
        if type(max_bytes) is not int or not 1<=max_bytes<=2*1024**3 or not 1<=transfer_seconds<=600:raise ValueError('bounded transfer policy required')
        self.client=client;self.store=TaskStore(jobs_root,job_id);self.tools=tools
        self.max_bytes=max_bytes;self.transfer_seconds=transfer_seconds;self.license_validation=license_validation

    def _current(self,spec):
        manifest=self.client.manifest(spec.asset_id,spec.sha256)
        detail=self.client.asset(spec.asset_id)
        size=manifest.get('size_bytes');mime=manifest.get('media_type')
        if type(size) is not int or not 1<=size<=self.max_bytes:raise LibraryClientError('download_size_budget')
        expected=f'/v1/assets/{spec.asset_id}/content?sha256={spec.sha256}'
        if manifest.get('content')!=expected or manifest.get('etag')!='"'+spec.sha256+'"' or manifest.get('whole_file') is not True or mime not in SUFFIX:
            raise LibraryClientError('invalid_download_manifest')
        if detail.get('sha256')!=spec.sha256 or detail.get('revision')!=manifest.get('revision') or detail.get('size_bytes')!=size or detail.get('file_access')!='authorized':
            raise LibraryClientError('stale_asset_metadata',retryable=True)
        technical=manifest.get('technical') or {}
        if technical.get('report_state')!='ready':raise LibraryClientError('technical_metadata_unavailable',retryable=True)
        if detail.get('technical')!=technical or detail.get('transform')!=manifest.get('transform'):raise LibraryClientError('stale_asset_metadata',retryable=True)
        context=spec.context
        if technical.get('kind') in ('video','gif','animation'):
            duration=(technical.get('time_basis') or {}).get('duration_seconds')
            if context.source_interval is None or type(duration) not in (int,float) or context.source_interval.end_seconds>duration:
                raise LibraryClientError('current_file_interval_required')
            if abs(context.timeline.end_seconds-context.timeline.start_seconds-context.source_interval.end_seconds+context.source_interval.start_seconds)>1e-6:
                raise LibraryClientError('source_timeline_duration_mismatch')
        elif context.source_interval is not None:raise LibraryClientError('still_image_has_no_source_interval')
        return manifest,detail

    def _transfer(self,spec,manifest,part):
        size=manifest['size_bytes'];offset=self.store.path(part).stat().st_size if self.store.path(part).exists() else 0
        if offset>size:raise LibraryClientError('partial_size_exceeds_fixed_file')
        if offset==size:return offset
        headers={'Authorization':'Bearer '+self.client._token,'If-Match':manifest['etag'],'Accept-Encoding':'identity','Cache-Control':'no-store'}
        if offset:headers.update(Range=f'bytes={offset}-',**{'If-Range':manifest['etag']})
        if not self.client._slots.acquire(blocking=False):raise LibraryClientError('client_busy',retryable=True)
        started=time.monotonic();resumed=offset
        try:
            try:
                with self.client._opener.open(Request(self.client.origin+manifest['content'],headers=headers),timeout=self.client.settings.timeout_seconds) as response:
                    status=response.status
                    if response.headers.get('ETag')!=manifest['etag'] or response.headers.get('Content-Encoding','identity')!='identity':raise LibraryClientError('download_version_header_mismatch')
                    if status==206:
                        expected=f'bytes {offset}-{size-1}/{size}'
                        if response.headers.get('Content-Range')!=expected:raise LibraryClientError('download_range_mismatch')
                    elif status==200:
                        offset=0;resumed=0  # A server ignoring Range starts this same immutable file from zero.
                    else:raise LibraryClientError('download_status_mismatch')
                    if response.headers.get('Content-Length')!=str(size-offset):raise LibraryClientError('download_length_mismatch')
                    if response.headers.get_content_type()!=manifest['media_type']:raise LibraryClientError('download_media_type_mismatch')
                    mode='r+b' if self.store.path(part).exists() else 'x+b'
                    with self.store.open(part,mode) as stream:
                        if offset==0:stream.truncate(0)
                        stream.seek(offset);position=offset
                        read=getattr(response,'read1',response.read)
                        while True:
                            if time.monotonic()-started>self.transfer_seconds:raise LibraryClientError('download_deadline',retryable=True)
                            chunk=read(min(65536,size-position+1))
                            if not chunk:break
                            if position+len(chunk)>size:raise LibraryClientError('download_size_exceeded')
                            stream.write(chunk);position+=len(chunk)
                        stream.flush();os.fsync(stream.fileno())
                        if position!=size:raise LibraryClientError('download_incomplete',retryable=True)
            except HTTPError as exc:
                status=exc.code;exc.close()
                raise LibraryClientError('download_http_'+str(status),status=status,retryable=status in (429,502,503,504)) from None
            except (TimeoutError,URLError,OSError,HTTPException):raise LibraryClientError('download_interrupted',retryable=True) from None
        finally:self.client._slots.release()
        return resumed

    def _decode(self,name,manifest):
        path=self.store.path(name)
        with self.store.open(name,'rb') as stream:
            if media_type(stream)!=manifest['media_type']:raise LibraryClientError('download_signature_mismatch')
            # Keep the checked handle open during probing/decoding (Windows disallows replacement).
            result=probe_media(path,self.tools,ProbeOptions(timeout_seconds=20.0,max_input_bytes=self.max_bytes))
            technical=manifest['technical']
            if result.kind!=technical.get('kind') or result.display_width!=technical.get('display_width') or result.display_height!=technical.get('display_height'):
                raise LibraryClientError('download_technical_mismatch')
            if result.duration_seconds is not None and (result.duration_seconds>600 or technical.get('duration_seconds') is None or abs(result.duration_seconds-technical['duration_seconds'])>.05):
                raise LibraryClientError('download_duration_mismatch_or_budget')
            self.tools.ffmpeg_run(['-v','error','-xerror','-protocol_whitelist','file,pipe','-format_whitelist',FORMAT_WHITELIST,
                '-i',str(path),'-map','0:v:0','-f','null','-'],timeout=45,stdout_limit=1024)
        return {'kind':result.kind,'display_width':result.display_width,'display_height':result.display_height,'duration_seconds':result.duration_seconds,
            'signature_verified':True,'whole_file_decode_passed':True,'actual_media_observed':False}

    def fetch(self,spec:Acquisition):
        manifest,detail=self._current(spec)
        name=spec.sha256+'.'+SUFFIX[manifest['media_type']];part=spec.sha256+'.part'
        with self.store.lock(spec.sha256):
            reused=self.store.path(name).exists();resumed=0
            if not reused:
                resumed=self._transfer(spec,manifest,part)
                try:self.store.verify(part,spec.sha256,manifest['size_bytes'])
                except LibraryClientError:
                    self.store.path(part).unlink();raise
                decoded=self._decode(part,manifest)
            else:self.store.verify(name,spec.sha256,manifest['size_bytes']);decoded=self._decode(name,manifest)
            # Recheck authorization/metadata after potentially long transfer/decode, even on cache hits.
            current,current_detail=self._current(spec)
            if current!=manifest or current_detail!=detail:raise LibraryClientError('stale_asset_metadata',retryable=True)
            if not reused:
                try:os.link(self.store.path(part),self.store.path(name))
                except FileExistsError:pass
                self.store.path(part).unlink()
                self.store.verify(name,spec.sha256,manifest['size_bytes'])
            sources=[]
            for source in detail.get('sources',[]):
                parsed=SourceInput.model_validate({k:source.get(k) for k in ('label','reference','attribution','license_status')})
                sources.append({'source_id':source['source_id'],**parsed.model_dump(mode='json')})
            record={'schema_version':'d19-receipt-v1','provider':'media-tag','job_id':self.store.job_id,'service_origin':self.client.origin,
                'acquisition':spec.model_dump(mode='json'),'asset_id':spec.asset_id,'sha256':spec.sha256,'revision':manifest['revision'],
                'root_asset_id':detail['root_asset_id'],'parent_asset_id':detail['parent_asset_id'],
                'src':'edit/media-tag/'+name,'size_bytes':manifest['size_bytes'],'media_type':manifest['media_type'],'technical':manifest['technical'],
                'transform':manifest['transform'],'apply_transform_again':False,'sources':sources,'decode':decoded,
                'license_status':'unverified','privacy_gate':'passed_for_fetch_request','medical_status':'pending','current_use_review':'not_checked',
                'preview_only':True,'host_registration_performed':False}
            digest=self.store.write_json('receipt',record)
            return {'receipt_sha256':digest,'local_path':str(self.store.path(name)),'receipt':record,'reused':reused,'resumed_bytes':resumed}

    def prepare_preview(self,check:CurrentUseCheck):
        receipt=self.store.read_json('receipt',check.receipt_sha256)
        spec=Acquisition.model_validate_json(encode(receipt['acquisition']))
        if check.decision!='pass' or not check.actual_media_observed:raise LibraryClientError('current_use_check_not_passed')
        if check.source_sha256!=spec.sha256 or check.context!=spec.context:raise LibraryClientError('current_use_binding_mismatch')
        if check.checks['license'].status=='skipped' and self.license_validation:raise LibraryClientError('license_check_cannot_be_skipped')
        for path,expected in ((spec.context.transcript_path,spec.context.transcript_sha256),(spec.context.candidate_path,spec.context.candidate_sha256)):
            if not path.startswith('edit/'):raise LibraryClientError('invalid_use_document_path')
            with self.store.backend.open_read(path) as stream:raw=stream.read(4*1024**2+1)
            if len(raw)>4*1024**2 or sha256(raw).hexdigest()!=expected:raise LibraryClientError('current_use_document_hash_mismatch')
        for evidence in check.evidence:
            if not evidence.path.startswith('edit/') or evidence.path.endswith(('.local.json','.local.txt')):raise LibraryClientError('invalid_check_evidence')
            with self.store.backend.open_read(evidence.path) as stream:
                digest=sha256();size=0
                while chunk:=stream.read(65536):
                    digest.update(chunk);size+=len(chunk)
                    if size>self.max_bytes:raise LibraryClientError('check_evidence_byte_budget')
            if digest.hexdigest()!=evidence.sha256:raise LibraryClientError('check_evidence_hash_mismatch')
        latest=self.fetch(spec)
        if latest['receipt_sha256']!=check.receipt_sha256:raise LibraryClientError('current_use_receipt_changed')
        proposal={'schema_version':'d19-preview-handoff-v1','provider':'media-tag','job_id':self.store.job_id,'receipt_sha256':check.receipt_sha256,
            'file_sha256':spec.sha256,'src':receipt['src'].removeprefix('edit/'),'context':spec.context.model_dump(mode='json'),
            'current_use_check':check.model_dump(mode='json'),'check_authenticity':'caller_attestation_not_verified_by_adapter',
            'preview_only':True,'medical_status':'pending','final_export_allowed':False,'apply_transform_again':False,'host_registration_performed':False}
        digest=self.store.write_json('preview-handoff',proposal)
        return {'handoff_sha256':digest,'handoff':proposal}
