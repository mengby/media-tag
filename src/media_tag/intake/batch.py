from hashlib import sha256
import os
from pathlib import Path
import shutil
import time
from media_tag.catalog.service import encode
from media_tag.deployment.local import directory,lock,write_json
from media_tag.storage import StorageError
from media_tag.storage.filesystem import FilesystemBackend
from .inventory import load_batch,file_identity,hash_stream,csv_bytes,summary
from .models import IntakeError


def request_id(inventory,row):
    # Stable across job retries/new scans, while a changed source creates a new version.
    return 'intake-'+sha256(encode({'root':inventory.source_root,'path':row.key,'sha256':row.sha256}).encode()).hexdigest()


def display_name(row):
    name=Path(row.key).name
    # Fix only the uploaded display suffix when the source signature disagrees.
    # Original key/name/bytes remain in the pinned inventory and provenance.
    suffixes={'image/jpeg':{'.jpg','.jpeg','.jfif'},'image/png':{'.png'},'image/webp':{'.webp'},'image/gif':{'.gif'}}
    preferred={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp','image/gif':'.gif'}
    if row.signature_hint in suffixes and Path(name).suffix.lower() not in suffixes[row.signature_hint]:
        name+=preferred[row.signature_hint]
    if len(name)<=180:return name
    suffix=Path(name).suffix
    return name[:max(1,150-len(suffix))]+'-'+row.sha256[:16]+suffix


def _verified(client,asset_id,row):
    detail=client.call('GET','/v1/assets/'+asset_id)
    if detail.get('asset_id')!=asset_id or detail.get('sha256')!=row.sha256 or detail.get('size_bytes')!=row.size_bytes:
        raise IntakeError('intake_asset_binding_changed')
    member=client.call('GET','/ui/library/'+asset_id)
    if member.get('active') is not True:raise IntakeError('intake_membership_inactive')
    return detail,member


def apply(batch,client,*,limit=None,destination_root=None):
    batch,inventory,progress=load_batch(batch)
    source=directory(Path(inventory.source_root))
    if source.is_relative_to(batch) or batch.is_relative_to(source):raise IntakeError('source_and_batch_roots_overlap')
    if limit is not None and (type(limit) is not int or limit<1):raise IntakeError('invalid_batch_limit')
    if destination_root is not None:
        destination=directory(destination_root)
        if source.is_relative_to(destination) or destination.is_relative_to(source):raise IntakeError('source_and_runtime_overlap')
        required=3*sum(r.size_bytes for r in inventory.entries if r.state=='ready')+2*1024**3
        if shutil.disk_usage(destination).free<required:raise IntakeError('intake_destination_reserve')
    actor=client.editor()
    with lock(batch,name='import.lock'):
        # Re-read after owning the batch; another importer cannot lose successful rows.
        batch,inventory,progress=load_batch(batch)
        if progress.get('service_origin',client.origin)!=client.origin or progress.get('actor_id',actor)!=actor:
            raise IntakeError('intake_service_or_actor_changed')
        progress.update(service_origin=client.origin,actor_id=actor,state='importing')
        write_json(batch,'progress.json',progress);backend=FilesystemBackend(source);processed=0
        try:
            for row in inventory.entries:
                if row.state!='ready':continue
                previous=progress['rows'].get(row.key,{})
                complete=previous.get('state')=='imported' and previous.get('job_id') and not previous.get('dispatch_error')
                if limit is not None and processed>=limit and not complete:break
                if not complete:processed+=1
                current=dict(previous);progress['rows'][row.key]=current
                try:
                    # Open read-only and verify the frozen whole file before any server mutation.
                    with backend.open_read(row.key) as stream:
                        before=os.fstat(stream.fileno())
                        if file_identity(before)!=(row.size_bytes,row.mtime_ns,row.device,row.inode) or hash_stream(stream)!=row.sha256:
                            raise IntakeError('source_changed_since_inventory')
                        if complete:
                            detail,_=_verified(client,previous['asset_id'],row)
                            current.update(source_reverified=True,reused=True,index_status=detail.get('index_status','ai_draft'));continue
                        current.update(state='importing',source_sha256=row.sha256,upload_name=display_name(row))
                        write_json(batch,'progress.json',progress)
                        generation=current.get('upload_generation',0)
                        base_request=request_id(inventory,row)
                        body={'request_id':base_request+('-r'+str(generation) if generation else ''),'filename':display_name(row),'size_bytes':row.size_bytes,
                              'sha256':row.sha256,'purpose':'library','source':{'label':'本地导入：'+row.key[:1800],
                              'attribution':'只读源文件SHA256='+row.sha256+'；源根目录和原相对路径见本批次inventory.json','license_status':'unverified'}}
                        upload=client.call('POST','/uploads',payload=body)
                        if upload.get('state') in ('rejected','cancelled'):
                            history=current.setdefault('closed_uploads',[])
                            history.append({'upload_id':upload['upload_id'],'state':upload['state'],'error_code':upload.get('error_code')})
                            generation+=1;current['upload_generation']=generation;write_json(batch,'progress.json',progress)
                            body['request_id']=base_request+'-r'+str(generation)
                            upload=client.call('POST','/uploads',payload=body)
                            if upload.get('state') in ('rejected','cancelled'):raise IntakeError('intake_upload_closed_retry_same_batch')
                        if upload.get('sha256')!=row.sha256 or upload.get('size_bytes')!=row.size_bytes:raise IntakeError('intake_upload_binding_changed')
                        current.update(upload_id=upload['upload_id']);write_json(batch,'progress.json',progress)
                        offset=upload['offset'];chunk_size=upload['chunk_bytes']
                        if type(offset) is not int or not 0<=offset<=row.size_bytes or type(chunk_size) is not int or not 1<=chunk_size<=8*1024**2:
                            raise IntakeError('invalid_upload_offset_or_budget')
                        current['resumed_bytes']=offset;stream.seek(offset)
                        while offset<row.size_bytes:
                            chunk=stream.read(min(chunk_size,row.size_bytes-offset))
                            if not chunk:raise IntakeError('source_truncated_during_upload')
                            reply=client.call('PUT',f"/uploads/{upload['upload_id']}/chunks?offset={offset}",content=chunk,headers={'X-Chunk-SHA256':sha256(chunk).hexdigest()})
                            expected=offset+len(chunk)
                            if reply.get('offset')!=expected:raise IntakeError('upload_offset_changed')
                            offset=expected;current['offset']=offset;write_json(batch,'progress.json',progress)
                        if hash_stream(stream)!=row.sha256 or file_identity(os.fstat(stream.fileno()))!=file_identity(before):
                            raise IntakeError('source_changed_during_upload')
                        receipt=client.call('POST',f"/uploads/{upload['upload_id']}/complete")
                        if receipt.get('state')!='completed' or not receipt.get('asset_id'):raise IntakeError('intake_not_completed')
                        detail,_=_verified(client,receipt['asset_id'],row)
                        current.update(state='imported',asset_id=receipt['asset_id'],job_id=receipt.get('job_id'),
                            dispatch_error=receipt.get('dispatch_error'),source_reverified=True,reused=upload.get('state')=='completed',
                            index_status=detail.get('index_status','ai_draft'),license_status='unverified',medical_confirmation=False)
                        current.pop('error_code',None)
                except (OSError,StorageError,ValueError,IntakeError) as exc:
                    current.update(state='failed',error_code=exc.code if isinstance(exc,IntakeError) else 'intake_source_unreadable_or_unsafe')
                    if isinstance(exc,IntakeError) and (exc.status in (401,403) or exc.retryable):raise
                finally:write_json(batch,'progress.json',progress)
        finally:
            result=summary(inventory)
            ready=[progress['rows'].get(r.key,{}) for r in inventory.entries if r.state=='ready']
            result.update(imported=sum(r.get('state')=='imported' for r in ready),import_failed=sum(r.get('state')=='failed' for r in ready),
                pending=sum(r.get('state') not in ('imported','failed') for r in ready),
                analysis_queued=sum(bool(r.get('job_id')) for r in ready if r.get('state')=='imported'),
                dispatch_pending=sum(not r.get('job_id') or bool(r.get('dispatch_error')) for r in ready if r.get('state')=='imported'),
                service_origin=client.origin,actor_id=actor,medical_confirmation=False,license_status='unverified',
                review_assignment_required=False,transport_retries=getattr(client,'retry_count',0))
            result['state']='imported' if result['imported']==result['ready'] and not result['failed'] and not result['dispatch_pending'] else 'partial'
            progress['state']=result['state'];write_json(batch,'progress.json',progress);write_json(batch,'result.json',result)
            rows=[{'source_key':r.key,'sha256':r.sha256,**progress['rows'].get(r.key,{'state':r.state,'error_code':r.error_code})} for r in inventory.entries]
            (batch/'mapping.csv').write_bytes(csv_bytes(rows,['source_key','sha256','upload_name','state','asset_id','upload_id','job_id','error_code']))
        return result


def inspect(batch,client=None):
    batch,inventory,progress=load_batch(batch);result=summary(inventory)
    result['state']=progress['state']
    result['rows']=[{'source_key':r.key,**progress['rows'].get(r.key,{'state':r.state})} for r in inventory.entries]
    if client:
        actor=client.editor()
        if actor!=progress.get('actor_id') or client.origin!=progress.get('service_origin'):raise IntakeError('intake_service_or_actor_changed')
        jobs={}
        for row in result['rows']:
            if row.get('job_id') and row['job_id'] not in jobs:jobs[row['job_id']]=client.call('GET','/jobs/'+row['job_id'])
        result['analysis_jobs']=jobs
    return result
