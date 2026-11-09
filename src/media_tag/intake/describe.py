"""Add only missing/failed visual-field stages to the imported batch's existing jobs."""
from media_tag.deployment.local import write_json
from .inventory import load_batch
from .models import IntakeError


def enqueue(batch,client,*,limit=None):
    batch,inventory,progress=load_batch(batch);actor=client.editor()
    if progress.get('state')!='imported' or progress.get('actor_id')!=actor or progress.get('service_origin')!=client.origin:
        raise IntakeError('description_batch_binding_changed')
    if limit is not None and (type(limit) is not int or limit<1):raise IntakeError('invalid_batch_limit')
    result={'state':'queued','added':0,'reused':0,'failed':0,'rows':{}}
    for source in inventory.entries:
        if source.state!='ready':continue
        if limit is not None and result['added']>=limit:break
        previous=progress['rows'][source.key]
        try:
            detail=client.call('GET','/v1/assets/'+previous['asset_id'])
            if detail['sha256']!=source.sha256:raise IntakeError('description_source_changed')
            reply=client.call('POST','/search/assets/'+previous['asset_id']+'/visual-fields',payload={
                'job_id':previous['job_id'],'expected_revision':detail['revision'],'source_sha256':source.sha256})
            result['rows'][source.key]={'asset_id':previous['asset_id'],**reply}
            result['reused' if reply['reused'] else 'added']+=1
        except IntakeError as exc:
            result['rows'][source.key]={'state':'failed','error_code':exc.code};result['failed']+=1
            if exc.retryable or exc.status in (401,403):raise
        finally:write_json(batch,'visual-fields-queue.json',result)
    if result['failed']:result['state']='partial'
    write_json(batch,'visual-fields-queue.json',result)
    return {k:v for k,v in result.items() if k!='rows'}
