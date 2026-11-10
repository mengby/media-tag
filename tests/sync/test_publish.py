"""Fault-injected orchestration checks; actual export/HTTP/OCR has a separate rehearsal."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from media_contracts import Blob,Manifest,canonical,digest
from media_tag.sync.bridge import Bridge,Settings
from media_tag.sync.client import SyncError
from test_state import event,page


@pytest.fixture
def bridge(tmp_path,monkeypatch):
    from media_tag.sync import bridge as module
    token=tmp_path/'token';token.write_text('synthetic-token')
    settings=Settings.model_validate_json(json.dumps(dict(state_root=str(tmp_path/'state'),cloud=dict(base_url='http://127.0.0.1:1234',token_file=str(token)),
        owners={'alice':dict(local_actor_id='local-alice',client=dict(base_url='http://127.0.0.1:1235',token_file=str(token)))},
        source=dict(database=str(tmp_path/'source.db'),workspace=str(tmp_path/'workspace'),object_store=str(tmp_path/'objects'),actor_id='publisher',producer_revision='a'*40),
        publisher=dict(base_url='http://127.0.0.1:1234',token_file=str(token),expected_active_release=None))))
    class Cloud:
        def __init__(self):self.scope=dict(release_id='existing',asset_ids=['asset-existing']);self.releases={};self.acks={};self.fail_ack=True;self.pushes=0
        def close(self):pass
        def json(self,method,path,*,payload=None,missing_ok=False):
            if path=='/internal/sync/publication-scope':return self.scope
            if '/releases/' in path:return self.releases.get(path.rsplit('/',1)[1])
            if path=='/internal/sync/receipts':
                key=(payload['event_id'],payload['phase']);self.acks[key]=payload
                if self.fail_ack and key==('event-2','cloud_active'):self.fail_ack=False;raise SyncError('lost_response_after_commit')
                return dict(event_id=payload['event_id'],phase=payload['phase'])
            raise AssertionError(path)
    cloud=Cloud();b=Bridge(settings,cloud=cloud)
    b.state.receive_page(page(event(),event(2)))
    for n in (1,2):b.state.update('event-'+str(n),phase=2,asset_id='asset-'+str(n),classification='new_bytes',tag_proof=canonical([dict(stage='ocr',model_revision='r1',report_sha256='a'*64)]).decode())
    builds=[]
    def build(settings,destination):
        builds.append(settings);destination.mkdir();ref=Blob(sha256='a'*64,size_bytes=1)
        m=Manifest(release_id=settings.release_id,parent_release_id=settings.parent_release_id,producer_revision='a'*40,catalog=ref,objects=(ref,));(destination/'manifest.json').write_bytes(canonical(m));return m
    def push(path,settings):
        cloud.pushes+=1;m=Manifest.model_validate_json((Path(path)/'manifest.json').read_bytes())
        cloud.releases[m.release_id]=dict(adopted=True,manifest_sha256=digest(m));cloud.scope=dict(release_id=m.release_id,asset_ids=list(builds[-1].asset_ids))
    monkeypatch.setattr(module,'build_bundle',build);monkeypatch.setattr(module,'push_bundle',push)
    return SimpleNamespace(b=b,cloud=cloud,builds=builds,module=module,push=push)


def test_partial_ack_loss_does_not_republish_or_leave_completed_errors(bridge):
    e=bridge;e.b.publish()
    assert e.cloud.pushes==1 and set(e.builds[0].asset_ids)=={'asset-existing','asset-1','asset-2'}
    assert e.b.state.row('event-1')['phase']==4 and e.b.state.row('event-1')['last_error'] is None
    assert e.b.state.row('event-2')['phase']==3
    # Active may have moved after adoption. Recover historical adoption; no rollback.
    e.cloud.scope={'release_id':'later','asset_ids':['asset-existing','asset-1','asset-2','asset-other']}
    e.b.publish();assert e.cloud.pushes==1
    assert e.b.state.summary()['pending']==0 and e.b.state.summary()['errors']==[]
    assert e.cloud.scope['release_id']=='later'


def test_activation_response_lost_is_recovered(bridge,monkeypatch):
    e=bridge;e.cloud.fail_ack=False
    def lose(path,settings):e.push(path,settings);raise SyncError('lost_activation_response')
    monkeypatch.setattr(e.module,'push_bundle',lose);e.b.publish();assert e.cloud.pushes==1
    assert e.b.state.row('event-1')['phase']==2
    e.b.publish();assert e.b.state.summary()['cloud_active']==2 and e.cloud.pushes==1


def test_cas_conflict_rebases_on_current_catalogue(bridge,monkeypatch):
    e=bridge;e.cloud.fail_ack=False;first=[True]
    def race(path,settings):
        if first[0]:first[0]=False;e.cloud.scope={'release_id':'concurrent','asset_ids':['asset-existing','asset-concurrent']};raise SyncError('cas_conflict')
        e.push(path,settings)
    monkeypatch.setattr(e.module,'push_bundle',race)
    e.b.publish();assert e.b.state.summary()['pending']==2
    e.b.publish();assert e.b.state.summary()['cloud_active']==2
    assert set(e.builds[-1].asset_ids)=={'asset-existing','asset-concurrent','asset-1','asset-2'}


def test_repeated_build_failure_does_not_accumulate_media(bridge,monkeypatch):
    e=bridge
    def failed(settings,destination):destination.mkdir();(destination/'generated.media').write_bytes(b'x'*1024);raise ValueError('bad_report')
    monkeypatch.setattr(e.module,'build_bundle',failed)
    for _ in range(3):e.b.publish()
    assert not list(e.b.state.bundles.glob('sync-*')) and e.cloud.pushes==0
    assert e.b.state.summary()['pending']==2


def test_bridge_refuses_state_containing_source_database(bridge):
    s=bridge.b.settings.model_dump(mode='json');s['source']['database']=str(Path(s['state_root'])/'bundles'/'source.db')
    with pytest.raises(ValueError):Settings.model_validate_json(json.dumps(s))


def test_cas_switch_then_rollback_to_old_parent_does_not_collide(bridge,monkeypatch):
    e=bridge;e.cloud.fail_ack=False;attempt=[0]
    def race(path,settings):
        attempt[0]+=1
        if attempt[0]==1:e.cloud.scope={'release_id':'concurrent','asset_ids':['asset-existing']};raise SyncError('race1')
        if attempt[0]==2:e.cloud.scope={'release_id':'existing','asset_ids':['asset-existing']};raise SyncError('rollback')
        e.push(path,settings)
    monkeypatch.setattr(e.module,'push_bundle',race)
    for _ in range(3):e.b.publish()
    assert e.b.state.summary()['cloud_active']==2 and len({s.release_id for s in e.builds})==3
