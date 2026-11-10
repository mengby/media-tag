"""Proofs must come from released, owner/source-bound jobs and current real bytes."""
import json
from types import SimpleNamespace
import pytest
from media_contracts import IntakeEvent,IntakeSource,Blob
from media_tag.sync.bridge import tag_proofs
from media_tag.sync.client import SyncError
from search.conftest import env,sought
from search.test_ai_drafts import modeled


def arguments(m):
    return (SimpleNamespace(database=str(m.env.database.path),workspace=str(m.env.router.workspace)),
        IntakeEvent(event_id='event-1',feed_id='feed-1',sequence=1,upload_id='upload-1',actor_id='cloud-owner',filename='synthetic.png',
            blob=Blob(sha256=m.sought.asset.sha256,size_bytes=m.sought.asset.size_bytes),source=IntakeSource()),
        {'job_id':m.job['job_id'],'asset_id':m.sought.asset.asset_id,'local_owner_id':m.env.owner.actor_id})


def test_actual_artifacts_required_and_owner_is_not_interchangeable(modeled):
    m=modeled;source,event,row=arguments(m)
    proof=tag_proofs(source,event,row,('ocr',));assert proof[0]['stage']=='ocr'
    with pytest.raises(SyncError,match='binding_changed'):tag_proofs(source,event,row|{'local_owner_id':'different'},('ocr',))
    with pytest.raises(SyncError,match='missing'):tag_proofs(source,event,row,('visual_fields',))
    m.reports['ocr'].write_bytes(b'changed report')
    with pytest.raises(ValueError):tag_proofs(source,event,row,('ocr',))


def test_not_released_is_not_tagged(modeled):
    m=modeled;args=arguments(m)
    with m.env.database.transaction(write=True) as c:
        c.execute("UPDATE router_attempts SET released_at=NULL WHERE task_id IN (SELECT task_id FROM router_tasks WHERE job_id=? AND json_extract(spec_json,'$.stage')='ocr')",(m.job['job_id'],))
    with pytest.raises(SyncError,match='not_ready'):tag_proofs(*args,('ocr',))
