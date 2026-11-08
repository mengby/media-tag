"""Explicit new editing jobs, external acquisition storage and live provenance checks."""
from contextlib import contextmanager
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
from uuid import uuid4
from typing import Literal
from media_tag.catalog.service import encode
from media_tag.contracts.models import ContractModel,validate_relative_path
from media_tag.deployment.local import directory,lock,write_json
from media_tag.storage.filesystem import FilesystemBackend,_opened_path
from media_tag.storage import StorageError
from media_tag.search.public import AssetSearch
from media_tag.workers.probe import Toolchain
from .http import LibraryClient,LibraryClientError,load_settings
from .models import Acquisition
from .provider import TaskProvider


class EditingSettings(ContractModel):
    schema_version: Literal['d19-editing-config-v1']='d19-editing-config-v1'
    host_root: str
    asset_root: str
    client_config: str
    python: str
    tool_dir: str


def read(path,limit=4*1024**2):
    path=Path(path);backend=FilesystemBackend(directory(path.parent))
    with backend.open_read(path.name) as stream:raw=stream.read(limit+1)
    if len(raw)>limit:raise LibraryClientError('editing_input_byte_budget')
    return raw


def digest(path):
    path=Path(path);result=sha256();size=0
    with FilesystemBackend(directory(path.parent)).open_read(path.name) as stream:
        while chunk:=stream.read(1024**2):
            size+=len(chunk)
            if size>512*1024**2:raise LibraryClientError('editing_file_byte_budget')
            result.update(chunk)
    return result.hexdigest()


class EditingSource:
    def __init__(self,config):
        self.settings=EditingSettings.model_validate_json(read(config,65536))
        s=self.settings;self.host=directory(s.host_root);self.assets=directory(s.asset_root)
        self.jobs=directory(self.host/'jobs')
        if self.assets==self.host or self.assets.is_relative_to(self.host) or self.host.is_relative_to(self.assets):
            raise LibraryClientError('separate_acquisition_directory_required')
        self.client=LibraryClient(load_settings(s.client_config));self.tools=Toolchain(Path(s.tool_dir))
        os.environ['PATH']=str(directory(s.tool_dir))+os.pathsep+os.environ.get('PATH','')

    def marker(self,job_id):
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,100}',job_id) is None:raise LibraryClientError('invalid_editing_job_id')
        return {'schema_version':'d19-editing-job-v1','job_id':job_id,'host_root':str(self.host),'asset_root':str(self.assets)}

    def init_job(self,job_id):
        marker=self.marker(job_id);task=FilesystemBackend(self.jobs,writable=True)._resolve(job_id,allow_missing=True)
        try:task.mkdir()
        except FileExistsError:raise LibraryClientError('existing_editing_job_refused') from None
        write_json(task,'media-tag-job.json',marker)
        (task/'README.md').write_text('New editing job with explicit media-tag source. Original raw files remain read only. No medical approval or final export authorization.\n',encoding='utf8')
        (task/'edit').mkdir();(task/'edit/README.md').write_text('Task-local editing outputs and checked source provenance; preview/export policy remains owned by the editing project.\n',encoding='utf8')
        return {'job_id':job_id,'new_job':True,'medical_status':'pending','final_export_allowed':False}

    def task(self,job_id):
        marker=self.marker(job_id);task=directory(self.jobs/job_id)
        try:actual=json.loads(read(task/'media-tag-job.json'))
        except (StorageError,ValueError,OSError):raise LibraryClientError('unmanaged_editing_job_refused') from None
        if actual!=marker:raise LibraryClientError('unmanaged_editing_job_refused')
        directory(task/'edit');return task

    @contextmanager
    def owned(self,job_id):
        task=self.task(job_id)
        with lock(task,name='media-tag.lock'):yield task,FilesystemBackend(task,writable=True)

    def host_file(self,task,key):
        validate_relative_path(key)
        if not key.startswith('edit/'):raise LibraryClientError('editing_file_outside_edit')
        return FilesystemBackend(task)._resolve(key)

    def use_documents(self,task,spec):
        c=spec.context
        if c.transcript_path!='edit/cut.transcript.json' or not re.fullmatch(r'edit/automation/runs/[A-Za-z0-9_-]+/candidate\.json',c.candidate_path):
            raise LibraryClientError('editing_run_documents_required')
        for key,expected in ((c.transcript_path,c.transcript_sha256),(c.candidate_path,c.candidate_sha256)):
            if digest(self.host_file(task,key))!=expected:raise LibraryClientError('editing_document_changed')

    def provider(self,job_id,spec):
        jobs=self.assets/'jobs';jobs.mkdir(exist_ok=True)
        if not (jobs/'README.md').exists():(jobs/'README.md').write_text('Private per-editing-job immutable downloads and provenance; never a source approval.\n',encoding='utf8')
        key='editing-'+sha256((str(self.host)+'/'+job_id+'/'+encode(spec.model_dump(mode='json'))).encode()).hexdigest()[:32]
        return TaskProvider(self.client,jobs,key,self.tools)

    def copy_new(self,source,backend,key,expected):
        validate_relative_path(key);target=backend._resolve(key,allow_missing=True)
        if target.exists():
            if digest(target)!=expected:raise LibraryClientError('editing_cached_file_corrupt')
            return target
        target.parent.mkdir(parents=True,exist_ok=True);backend._resolve(key,allow_missing=True)
        parent=target.parent
        while parent!=backend.root:
            description=parent/'README.md'
            if not description.exists():
                with description.open('x',encoding='utf8') as stream:stream.write('Task-local source documents or media. Preserve original bytes, provenance and current-use checks. No medical or final export approval.\n')
            parent=parent.parent
        temporary=target.with_name('write-'+uuid4().hex+'.tmp')
        try:
            with FilesystemBackend(source.parent).open_read(source.name) as incoming,temporary.open('xb') as output:
                if _opened_path(output,temporary)!=temporary:raise LibraryClientError('unsafe_editing_handle')
                shutil.copyfileobj(incoming,output,1024**2);output.flush();os.fsync(output.fileno())
            if digest(temporary)!=expected:raise LibraryClientError('editing_copy_hash_mismatch')
            try:os.link(temporary,target)
            except FileExistsError:pass
        finally:temporary.unlink(missing_ok=True)
        if digest(target)!=expected:raise LibraryClientError('editing_copy_hash_mismatch')
        return target

    def search(self,job_id,value):
        spec=AssetSearch.model_validate_json(encode(value))
        with self.owned(job_id) as (task,backend):
            result=self.client.search(spec)
            record={'schema_version':'d19-editing-search-v1','provider':'media-tag','query':spec.model_dump(mode='json'),'result':result}
            key=sha256(encode(record).encode()).hexdigest()
            folder=task/'edit/media-tag';folder.mkdir(exist_ok=True)
            if not (folder/'README.md').exists():(folder/'README.md').write_text('Internal search records and provenance. Candidate results do not approve current use.\n',encoding='utf8')
            write_json(folder,'search-'+key+'.json',record)
            return result|{'search_record':'edit/media-tag/search-'+key+'.json'}

    def fetch(self,job_id,value):
        if set(value)!={'id','acquisition'} or re.fullmatch(r'[A-Za-z0-9_-]{1,100}',str(value['id'])) is None:
            raise LibraryClientError('editing_fetch_input_invalid')
        spec=Acquisition.model_validate_json(encode(value['acquisition']));identity=value['id']
        with self.owned(job_id) as (task,backend):
            self.use_documents(task,spec)
            registry=task/'edit/media_assets.json'
            if registry.exists():
                old=json.loads(read(registry))
                if old.get('transcript_sha256')!=spec.context.transcript_sha256:raise LibraryClientError('editing_registry_transcript_changed')
                existing=next((a for a in old['assets'] if a['id']==identity),None)
                if existing:
                    if (existing.get('media_tag') or {}).get('acquisition')!=spec.model_dump(mode='json'):
                        raise LibraryClientError('editing_asset_id_already_registered')
                    item={'id':identity,'type':existing['type'],'mode':'fullscreen' if existing['type']=='video' else 'panel',
                        'src':existing['src'],'start':spec.context.timeline.start_seconds,'end':spec.context.timeline.end_seconds,
                        'candidate':existing['approval']['status']=='candidate'}
                    if existing['type']=='video':item['sourceStart']=spec.context.source_interval.start_seconds
                    self.validate(job_id,{'item':item,'asset':existing})
                    provider=self.provider(job_id,spec);binding=existing['media_tag']
                    receipt=provider.store.read_json('receipt',binding['receipt_sha256'])
                    source=provider.store.backend._resolve(receipt['src'])
                    provider.store.verify(source.name,spec.sha256,receipt['size_bytes'])
                    return {'item':item,'receipt_sha256':binding['receipt_sha256'],'external_path':str(source),
                        'host_path':str(self.host_file(task,'edit/'+item['src'])),'reused':True,
                        'medical_status':'pending','final_export_allowed':False}
            provider=self.provider(job_id,spec)
            for key,expected in ((spec.context.transcript_path,spec.context.transcript_sha256),(spec.context.candidate_path,spec.context.candidate_sha256)):
                self.copy_new(self.host_file(task,key),provider.store.backend,key,expected)
            fetched=provider.fetch(spec);receipt=fetched['receipt']
            if receipt['media_type'] not in ('image/png','image/jpeg','image/webp','video/mp4'):
                raise LibraryClientError('editing_format_requires_explicit_conversion')
            source=Path(fetched['local_path']);src='media/media-tag-'+spec.sha256+source.suffix
            self.copy_new(source,backend,'edit/'+src,spec.sha256)
            sys.path.insert(0,str(self.host/'scripts'))
            register=importlib.import_module('register_candidate').register
            binding={'schema_version':'d19-editing-binding-v1','receipt_sha256':fetched['receipt_sha256'],
                'acquisition':spec.model_dump(mode='json'),'external_job_id':provider.store.job_id}
            folder=task/'edit/media-tag';folder.mkdir(exist_ok=True)
            if not (folder/'README.md').exists():(folder/'README.md').write_text('Internal search, original acquisition receipts and host privacy lineage. No automatic approval.\n',encoding='utf8')
            # The editing project's privacy processor remains authoritative and may write a derivative.
            kind='video' if receipt['media_type']=='video/mp4' else 'image'
            item=register(task/'edit',identity,kind,src,
                self.client.origin+f'/v1/assets/{spec.asset_id}', 'Current-use model checks required; license remains unverified.',
                extra={'provider':'media-tag','media_tag':binding,'license_status':'unverified','source_records':receipt['sources'],
                       'medical_status':'pending','apply_transform_again':False,'quote':spec.context.quote,
                       'purpose':spec.context.purpose,'unit_id':spec.context.unit_id})
            c=spec.context;item.update(start=c.timeline.start_seconds,end=c.timeline.end_seconds)
            if kind=='video':item['sourceStart']=c.source_interval.start_seconds
            self.use_documents(task,spec)
            write_json(folder,'receipt-'+fetched['receipt_sha256']+'.json',receipt)
            return {'item':item,'receipt_sha256':fetched['receipt_sha256'],'external_path':str(source),
                'host_path':str(self.host_file(task,'edit/'+item['src'])),'reused':fetched['reused'],
                'medical_status':'pending','final_export_allowed':False}

    def validate(self,job_id,value):
        if set(value)!={'item','asset'}:raise LibraryClientError('editing_validation_input_invalid')
        item,asset=value['item'],value['asset'];task=self.task(job_id)
        binding=asset.get('media_tag') or {}
        if binding.get('schema_version')!='d19-editing-binding-v1' or asset.get('provider')!='media-tag':raise LibraryClientError('editing_binding_missing')
        spec=Acquisition.model_validate_json(encode(binding['acquisition']))
        provider=self.provider(job_id,spec)
        if provider.store.job_id!=binding['external_job_id']:raise LibraryClientError('editing_external_job_mismatch')
        receipt=provider.store.read_json('receipt',binding['receipt_sha256'])
        if receipt['acquisition']!=spec.model_dump(mode='json'):raise LibraryClientError('editing_receipt_binding_changed')
        if receipt['service_origin']!=self.client.origin or asset.get('source_page')!=self.client.origin+f'/v1/assets/{spec.asset_id}' or asset.get('source_records')!=receipt['sources']:
            raise LibraryClientError('editing_source_provenance_changed')
        manifest,detail=provider._current(spec)
        sources=[{k:s.get(k) for k in ('source_id','label','reference','attribution','license_status')} for s in detail['sources']]
        if any(receipt[k]!=manifest[k] for k in ('revision','technical','transform')) or receipt['sources']!=sources:
            raise LibraryClientError('editing_source_metadata_changed')
        if item.get('id')!=asset.get('id') or item.get('src')!=asset.get('src') or item.get('type')!=asset.get('type'):
            raise LibraryClientError('editing_item_binding_changed')
        src=item['src'];validate_relative_path(src)
        if not src.startswith('media/'):raise LibraryClientError('editing_media_path_required')
        actual=digest(self.host_file(task,'edit/'+src))
        privacy=asset.get('eye_mosaic')
        if actual!=asset.get('sha256') or (privacy and (privacy.get('input_sha256')!=spec.sha256 or privacy.get('output_sha256')!=actual)) or not privacy and actual!=spec.sha256:
            raise LibraryClientError('editing_privacy_lineage_changed')
        sys.path.insert(0,str(self.host/'scripts'));privacy_module=importlib.import_module('media_privacy')
        if privacy_module.enabled() and not privacy:raise LibraryClientError('editing_privacy_processing_required')
        privacy_module.validate(privacy,actual)
        c=spec.context
        if (item.get('start'),item.get('end'))!=(c.timeline.start_seconds,c.timeline.end_seconds):raise LibraryClientError('editing_use_window_changed')
        if item.get('type')=='video' and item.get('sourceStart',0)!=c.source_interval.start_seconds:raise LibraryClientError('editing_source_interval_changed')
        if digest(task/'edit/cut.transcript.json')!=c.transcript_sha256:raise LibraryClientError('editing_transcript_changed')
        return {'state':'valid','provider':'media-tag','file_sha256':actual,'source_sha256':spec.sha256,'current_use_review':'not_checked'}


def execute(action,config,job_id,value=None):
    source=EditingSource(config)
    return source.init_job(job_id) if action=='init-job' else getattr(source,action)(job_id,value)
