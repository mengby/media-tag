"""Single resumable batch action: enqueue, wait for drafts, export unchanged template."""
import time
from media_tag.deployment.local import write_json
from .inventory import load_batch
from .models import IntakeError
from .drafts import materialize


def run(batch,client,*,notify=None,timeout_seconds=12*3600):
    batch,inventory,progress=load_batch(batch);actor=client.editor()
    if progress.get('actor_id')!=actor or progress.get('service_origin')!=client.origin or progress.get('state')!='imported':raise IntakeError('tag_batch_binding_changed')
    selection=[]
    for row in inventory.entries:
        if row.state!='ready':continue
        previous=progress['rows'][row.key];detail=client.call('GET','/v1/assets/'+previous['asset_id'])
        if detail['sha256']!=row.sha256:raise IntakeError('tag_source_changed')
        selection.append({'asset_id':previous['asset_id'],'expected_revision':detail['revision'],'source_sha256':row.sha256})
    scope={'assets':selection};submitted=client.call('POST','/search/auto-tag',payload=scope)
    if submitted.get('errors'):raise IntakeError('tag_some_sources_unavailable')
    started=time.monotonic();last=None
    while True:
        status=client.call('POST','/search/auto-tag/status',payload=scope)
        write_json(batch,'auto-tag-progress.json',status)
        if status!=last and notify:notify(status)
        last=status
        if status['state']=='ready':break
        if status['state']=='partial':raise IntakeError('tag_failed_stages_resume_same_batch')
        if time.monotonic()-started>=timeout_seconds:raise IntakeError('tag_wait_timeout_background_jobs_continue')
        time.sleep(10)
    result=materialize(batch,client)
    write_json(batch,'auto-tag-result.json',result)
    return result
