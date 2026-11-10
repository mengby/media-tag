"""Pull-only local bridge. Existing HTTP intake owns writes and existing Workers own AI."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from types import MethodType, SimpleNamespace
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from media_contracts import IntakeEvent, PHASES, SyncReceipt, TagProof, canonical, digest
from media_tag.client.http import ClientSettings
from media_tag.intake.client import IntakeClient
from media_tag.publish.bundle import ExportSettings, build_bundle
from media_tag.publish.client import PublishSettings, push_bundle
from .client import CloudClient, SyncError
from .state import State


class Owner(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    local_actor_id:str
    client:ClientSettings


class Source(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    database:str
    object_store:str
    workspace:str
    actor_id:str
    role:Literal['admin']='admin'
    producer_revision:str
    template_dir:str|None=None
    review:dict|None=None


class Settings(BaseModel):
    model_config=ConfigDict(extra='forbid',frozen=True)
    state_root:str
    cloud:ClientSettings
    owners:dict[str,Owner]
    source:Source
    publisher:PublishSettings
    required_stages:tuple[Literal['ocr','face','clip','visual_fields'],...]=('ocr',)
    page_size:int=Field(default=50,ge=1,le=100)

    @model_validator(mode='after')
    def explicit_scope(self):
        if not self.required_stages or len(set(self.required_stages))!=len(self.required_stages):raise ValueError('unique actual AI stages required')
        if self.cloud.base_url.rstrip('/')!=self.publisher.base_url.rstrip('/'):raise ValueError('publisher and sync must use one cloud origin')
        from media_tag.storage.filesystem import require_local_path
        for value in (self.state_root,self.source.database,self.source.workspace,self.source.object_store):
            if not Path(value).is_absolute():raise ValueError('absolute local paths required')
            require_local_path(Path(value))
        roots=[Path(self.state_root).resolve(),Path(self.source.workspace).resolve(),Path(self.source.object_store).resolve()]
        if any(roots[0].is_relative_to(r) or r.is_relative_to(roots[0]) for r in roots[1:]):raise ValueError('sync state must be separate from source storage and workspace')
        if Path(self.source.database).resolve().is_relative_to(roots[0]):raise ValueError('source database must be outside sync state')
        return self


@contextmanager
def readonly(path):
    c=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
    try:c.execute('BEGIN');yield c
    finally:c.close()


def tag_proofs(source,event,row,stages):
    from media_tag.contracts.models import TaskEnvelope,WorkerResult
    from media_tag.drafts.service import DraftIndex
    from media_tag.router.service import Router
    from media_tag.storage.filesystem import FilesystemBackend
    router=SimpleNamespace(files=FilesystemBackend(source.workspace),stages={s:SimpleNamespace(require_artifact=True) for s in stages},database=None)
    router._artifacts=MethodType(Router._artifacts,router);index=DraftIndex(router)
    with readonly(source.database) as c:
        job=c.execute('SELECT * FROM router_jobs WHERE job_id=? AND asset_id=? AND owner_id=? AND cancelled=0',(row['job_id'],row['asset_id'],row['local_owner_id'])).fetchone()
        if not job:raise SyncError('local_job_binding_changed')
        tasks=c.execute('''SELECT t.state,t.spec_json,a.envelope_json,a.result_json,a.released_at FROM router_tasks t
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.job_id=? AND t.active=1''',(row['job_id'],)).fetchall()
        proofs=[]
        for stage in stages:
            matches=[t for t in tasks if json.loads(t['spec_json'])['stage']==stage]
            if len(matches)!=1:raise SyncError('required_tag_stage_missing')
            t=matches[0]
            if t['state']!='succeeded' or t['released_at'] is None or not t['result_json']:raise SyncError('local_tag_not_ready')
            task=TaskEnvelope.model_validate_json(t['envelope_json']);result=WorkerResult.model_validate_json(t['result_json'])
            if task.asset_id!=row['asset_id'] or task.input_refs[0].sha256!=event.blob.sha256 or result.status!='succeeded':raise SyncError('local_tag_binding_changed')
            ref,report=index._report(task,result);index._project(task,result,ref,report)
            proofs.append(TagProof(stage=stage,model_revision=task.versions.model_revision,report_sha256=ref.sha256).model_dump(mode='json'))
        return proofs


class Bridge:
    def __init__(self,settings,*,cloud=None):
        self.settings=Settings.model_validate_json(settings.model_dump_json())
        s=self.settings
        scope={'cloud':s.cloud.base_url.rstrip('/'),'source':{k:str(Path(getattr(s.source,k)).resolve()) for k in ('database','workspace','object_store')},'publisher_actor':s.source.actor_id}
        self.state=State(s.state_root,scope);self.cloud=cloud or CloudClient(s.cloud)

    def close(self):self.cloud.close()

    def receipt(self,event,row,phase,*,release=None,manifest=None):
        owner=self.settings.owners[event.actor_id]
        payload=SyncReceipt(request_id='ack-'+digest([event.event_id,phase])[:40],feed_id=event.feed_id,event_id=event.event_id,
            phase=phase,source_sha256=event.blob.sha256,local_asset_id=row['asset_id'],local_owner_id=owner.local_actor_id,
            classification=row['classification'],tag_proof=tuple(json.loads(row['tag_proof'])) if phase!='registered' else (),
            release_id=release,manifest_sha256=manifest)
        reply=self.cloud.json('POST','/internal/sync/receipts',payload=payload.model_dump(mode='json'))
        if reply.get('event_id')!=event.event_id or reply.get('phase')!=phase:raise SyncError('receipt_ack_invalid')
        self.state.update(event.event_id,phase=PHASES.index(phase)+1,last_error=None)

    def import_event(self,row):
        event=IntakeEvent.model_validate_json(row['payload']);owner=self.settings.owners.get(event.actor_id)
        if owner is None:raise SyncError('owner_mapping_required')
        local=IntakeClient(owner.client)
        if local.editor()!=owner.local_actor_id:raise SyncError('local_identity_mismatch')
        self.state.bind_owner(event.actor_id,owner.local_actor_id,owner.client.base_url.rstrip('/'))
        if not row['local_upload']:
            upload=local.call('POST','/uploads',payload={'request_id':'cloud-'+event.upload_id,'filename':event.filename,
                'size_bytes':event.blob.size_bytes,'sha256':event.blob.sha256,'source':event.source.model_dump(mode='json'),'purpose':'library'})
            self.state.update(event.event_id,local_upload=upload['upload_id']);row=self.state.row(event.event_id)
        upload=local.call('GET','/uploads/'+row['local_upload'])
        if upload['state'] not in ('completed','validating','receiving'):raise SyncError('local_upload_'+upload['state'])
        if upload['state']=='receiving':
            path=self.cloud.download(event,self.state.downloads/(event.event_id+'.media'))
            offset=upload['offset']
            with path.open('rb') as stream:
                stream.seek(offset)
                while chunk:=stream.read(min(upload['chunk_bytes'],4*1024**2)):
                    upload=local.call('PUT',f"/uploads/{row['local_upload']}/chunks?offset={offset}",content=chunk,headers={'X-Chunk-Sha256':sha256(chunk).hexdigest()})
                    offset=upload['offset']
        if upload['state']!='completed' or not upload.get('job_id'):
            upload=local.call('POST','/uploads/'+row['local_upload']+'/complete')
        if not upload.get('job_id') or not upload.get('asset_id'):raise SyncError('local_dispatch_pending')
        with readonly(self.settings.source.database) as c:
            registered=c.execute("SELECT * FROM uploads WHERE upload_id=? AND owner_id=? AND state='completed'",(row['local_upload'],owner.local_actor_id)).fetchone()
            if not registered or registered['asset_id']!=upload['asset_id']:raise SyncError('local_catalog_binding_changed')
            classification=json.loads(registered['validation_json']).get('content_classification')
            if classification not in ('new_bytes','duplicate_bytes'):raise SyncError('local_classification_unavailable')
        self.state.update(event.event_id,asset_id=upload['asset_id'],job_id=upload['job_id'],classification=classification)
        row=self.state.row(event.event_id)
        if row['phase']==0:self.receipt(event,row,'registered');row=self.state.row(event.event_id)
        if row['phase']==1:
            job=local.call('GET','/jobs/'+row['job_id'])
            if job['state']!='succeeded' or job['resources']:raise SyncError('local_tag_not_ready')
            proof=tag_proofs(self.settings.source,event,{**row,'local_owner_id':owner.local_actor_id},self.settings.required_stages)
            self.state.update(event.event_id,tag_proof=canonical(proof).decode());row=self.state.row(event.event_id)
            self.receipt(event,row,'tagged')

    def publish(self):
        # Resume immutable attempts before preparing a new one. A lost activation
        # response is recovered by checking the cloud's durable adoption fact.
        with self.state.db() as c:batches=[dict(r) for r in c.execute("SELECT * FROM batches WHERE status='pending' ORDER BY rowid")]
        for batch in batches:self.publish_batch(batch['batch_id'],json.loads(batch['payload']))
        rows=[r for r in self.state.rows() if r['phase']==2 and not r['batch_id']]
        if not rows:return
        scope=self.cloud.json('GET','/internal/sync/publication-scope')
        ids=sorted(set(scope['asset_ids'])|{r['asset_id'] for r in rows})
        # Each new attempt is unique even if concurrent adoption + rollback returns
        # to a previously superseded parent. Retries reuse this persisted identity.
        release='sync-'+digest([scope,ids,[(r['event_id'],r['tag_proof']) for r in rows],self.settings.source.producer_revision])[:32]+'-'+uuid4().hex[:8]
        data={'release':release,'parent':scope['release_id'],'ids':ids,'events':[r['event_id'] for r in rows],'bundle':None,'manifest':None}
        with self.state.db(True) as c:
            c.execute("INSERT INTO batches VALUES(?,?,'pending')",(release,canonical(data).decode()))
            c.executemany('UPDATE events SET batch_id=? WHERE event_id=?',[(release,r['event_id']) for r in rows])
        self.publish_batch(release,data)

    def publish_batch(self,batch_id,data):
        try:
            remote=self.cloud.json('GET','/internal/sync/releases/'+batch_id,missing_ok=True)
            adopted=bool(remote and remote['adopted'])
            if not adopted:
                active=self.cloud.json('GET','/internal/sync/publication-scope')
                if active['release_id'] not in (data['parent'],batch_id):
                    with self.state.db(True) as c:
                        c.execute("UPDATE batches SET status='superseded' WHERE batch_id=?",(batch_id,))
                        c.execute('UPDATE events SET batch_id=NULL WHERE batch_id=? AND phase=2',(batch_id,))
                    return
                if not data['bundle']:
                    destination=self.state.bundles/(batch_id+'-'+uuid4().hex[:8])
                    settings=ExportSettings(**self.settings.source.model_dump(),asset_ids=tuple(data['ids']),release_id=batch_id,parent_release_id=data['parent'])
                    try:build_bundle(settings,destination)
                    except Exception:
                        # Only this bridge's generated incomplete attempt is removed.
                        # Repeated failures cannot accumulate full media copies.
                        import shutil
                        if destination.is_dir() and not destination.is_symlink() and destination.resolve().parent==self.state.bundles.resolve():
                            shutil.rmtree(destination)
                        raise
                    # build_bundle returns a summary; read validated wire bytes for its digest.
                    from media_contracts import Manifest
                    wire=Manifest.model_validate_json((destination/'manifest.json').read_bytes())
                    data.update(bundle=str(destination),manifest=digest(wire))
                    with self.state.db(True) as c:c.execute('UPDATE batches SET payload=? WHERE batch_id=?',(canonical(data).decode(),batch_id))
                settings=self.settings.publisher.model_copy(update={'expected_active_release':data['parent']})
                push_bundle(data['bundle'],settings)
                remote=self.cloud.json('GET','/internal/sync/releases/'+batch_id)
            if not remote or not remote['adopted'] or remote['manifest_sha256']!=data['manifest']:raise SyncError('publication_adoption_unverified')
            for identifier in data['events']:
                row=self.state.row(identifier);event=IntakeEvent.model_validate_json(row['payload'])
                if row['phase']==2:self.receipt(event,row,'published',release=batch_id,manifest=data['manifest']);row=self.state.row(identifier)
                if row['phase']==3:self.receipt(event,row,'cloud_active',release=batch_id,manifest=data['manifest'])
            with self.state.db(True) as c:
                c.execute("UPDATE batches SET status='done' WHERE batch_id=?",(batch_id,))
                c.execute('UPDATE events SET last_error=NULL WHERE batch_id=? AND phase=4',(batch_id,))
        except Exception as exc:
            code=exc.code if isinstance(exc,SyncError) else 'publication_retry_required'
            for identifier in data['events']:
                if self.state.row(identifier)['phase']<4:self.state.update(identifier,last_error=code)

    def once(self):
        with self.state.lock():
            page=self.cloud.json('GET',f'/internal/sync/events?after={self.state.cursor()}&limit={self.settings.page_size}')
            self.state.receive_page(page)
            for row in self.state.rows():
                if row['phase']>=2:continue
                try:self.import_event(row);self.state.update(row['event_id'],last_error=None)
                except Exception as exc:self.state.update(row['event_id'],last_error=getattr(exc,'code','local_sync_retry_required'))
            self.publish()
            return self.state.summary()
