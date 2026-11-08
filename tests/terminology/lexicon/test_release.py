from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import json
import sqlite3
import pytest
from media_tag.catalog import RevisionConflict
from media_tag.contracts.vocabulary import load_published_vocabulary
from media_tag.review.models import ReviewDeployment
from media_tag.review.policy import ReviewPolicy
from media_tag.storage import IntegrityError
from media_tag.terminology.contracts import Publish,Rollback
from .conftest import create,approve,publish,taxonomy,lexicon,option,concept


def publication(env,candidate,version='synthetic-v1',request='publish-test'):
    return Publish(request_id=request,expected_revision=candidate['revision'],expected_generation=env.service.active(principal=env.owner)['generation'],version=version,note='Synthetic publication')


def test_taxonomy_manifest_d06_consumer_and_hashes(env,tax):
    path=env.workspace/tax['manifest_ref']
    manifest=load_published_vocabulary(path,expected_kind='taxonomy',expected_version=tax['version'])
    assert manifest.published_by==env.publisher.actor_id
    config=ReviewDeployment(enabled=True,taxonomy_manifest=str(path),taxonomy_version=tax['version'],taxonomy_manifest_sha256=tax['manifest_sha256'],trusted_publishers=(env.publisher.actor_id,))
    policy=ReviewPolicy(env.contract,config)
    assert policy.options(policy.asset_fields['疾病/症状'])==('合成选项甲-仅测试',)
    detail=env.service.release(tax['release_id'],principal=env.manager)
    for name,digest in detail['bundle']['files'].items():assert sha256((path.parent/name).read_bytes()).hexdigest()==digest
    with env.database.transaction(write=True) as c:
        with pytest.raises(sqlite3.IntegrityError):c.execute("UPDATE terminology_releases SET version='mutated'")
        with pytest.raises(sqlite3.IntegrityError):c.execute('DELETE FROM terminology_releases')


@pytest.mark.parametrize('failed_file',['content.json','manifest.json','diff.json','review.json','inputs.json','result.json'])
def test_build_failure_preserves_old_head_retry_succeeds(env,tax,monkeypatch,failed_file):
    candidate=approve(env,create(env,taxonomy(option(),option('option-b',value='合成乙')),base=tax['release_id']))
    spec=publication(env,candidate,'synthetic-v2');before=env.service.active(principal=env.owner)
    original=env.service._write
    def fail(folder,name,raw):
        if name==failed_file:raise OSError('synthetic injected disk failure')
        return original(folder,name,raw)
    monkeypatch.setattr(env.service,'_write',fail)
    with pytest.raises(OSError):env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)
    assert env.service.active(principal=env.owner)==before
    assert env.service.get(candidate['candidate_id'],principal=env.owner)['state']=='approved'
    assert list((env.workspace/'jobs').glob('terminology-*/failure.json'))
    monkeypatch.setattr(env.service,'_write',original)
    result=env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)
    assert result['generation']==before['generation']+1
    assert env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)==result
    assert env.service.active(principal=env.owner)['generation']==result['generation']


def test_corruption_before_switch_rolls_back_and_download_never_ignores_hash(env,tax,monkeypatch):
    candidate=approve(env,create(env,lexicon(tax['release_id'])));spec=publication(env,candidate)
    original=env.service._write
    def corrupt(folder,name,raw):
        original(folder,name,raw)
        if name=='result.json':(folder/'content.json').write_bytes(b'{}')
    monkeypatch.setattr(env.service,'_write',corrupt)
    with pytest.raises(IntegrityError):env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)
    assert env.service.active(principal=env.owner)['lexicon_release_id'] is None
    monkeypatch.setattr(env.service,'_write',original)
    release=env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)
    (env.workspace/release['manifest_ref']).write_text('{}')
    with pytest.raises(IntegrityError):env.service.active(principal=env.owner)
    with pytest.raises(IntegrityError):env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)


def test_transaction_failure_after_pointer_update_is_not_published(env,tax,monkeypatch):
    candidate=approve(env,create(env,lexicon(tax['release_id'])));spec=publication(env,candidate)
    original=env.service._done
    def fail(*args,**kwargs):raise OSError('synthetic failure before transaction commit')
    monkeypatch.setattr(env.service,'_done',fail)
    with pytest.raises(OSError):env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)
    assert env.service.active(principal=env.owner)['generation']==1
    with env.database.transaction() as c:assert c.execute('SELECT count(*) FROM terminology_releases').fetchone()[0]==1
    monkeypatch.setattr(env.service,'_done',original)
    assert env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)['generation']==2


def test_paired_rollback_rechecks_hash_and_increments_generation(env,tax):
    lex=publish(env,lexicon(tax['release_id']))
    tax2=publish(env,taxonomy(option(),option('option-b',value='合成乙')),'synthetic-v2',tax['release_id'])
    assert env.service.active(principal=env.owner)['lexicon_release_id'] is None
    assert tax2['diff']['lexicon_deactivated'] and not tax2['diff']['visual_reanalysis']
    invalid=Rollback(request_id='bad-pair',expected_generation=3,taxonomy_release_id=tax2['release_id'],lexicon_release_id=lex['release_id'],note='Synthetic rollback')
    with pytest.raises(ValueError):env.service.rollback(invalid,principal=env.publisher)
    spec=invalid.model_copy(update={'request_id':'rollback','taxonomy_release_id':tax['release_id']})
    result=env.service.rollback(spec,principal=env.publisher)
    assert result['generation']==4 and result['lexicon_release_id']==lex['release_id']
    assert env.service.rollback(spec,principal=env.publisher)==result
    with pytest.raises(RevisionConflict):env.service.rollback(spec.model_copy(update={'request_id':'stale'}),principal=env.publisher)
    with env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM router_jobs').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM effective_values').fetchone()[0]==0


def test_concurrent_publication_same_base_one_winner_and_restart(env,tax):
    candidates=[approve(env,create(env,lexicon(tax['release_id']))) for _ in range(2)]
    specs=[publication(env,c,'synthetic-v'+str(i),request='concurrent-'+str(i)) for i,c in enumerate(candidates)]
    def action(i):
        try:return env.service.publish(candidates[i]['candidate_id'],specs[i],principal=env.publisher)
        except RevisionConflict:return 'conflict'
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(action,range(2)))
    assert sum(r=='conflict' for r in results)==1
    from media_tag.terminology.lexicon.service import TerminologyService
    restart=TerminologyService(env.router,env.config)
    assert restart.active(principal=env.owner)['generation']==2


def test_rename_diff_has_semantic_impact_no_visual_or_human_write(env,tax):
    lex=publish(env,lexicon(tax['release_id']))
    payload=lexicon(tax['release_id'],concepts=[concept(definition='Changed synthetic semantics')])
    candidate=create(env,payload,base=lex['release_id'])
    result=env.service.preview(candidate['candidate_id'],principal=env.manager)
    assert result['diff']['semantic_concepts']==['concept-a']
    assert result['diff']['affected_asset_ids']==[] and not result['diff']['visual_reanalysis'] and not result['diff']['human_values_rewritten']
    aliaschange=lexicon(tax['release_id'],aliases=[])
    second=create(env,aliaschange,base=lex['release_id'])
    assert env.service.preview(second['candidate_id'],principal=env.manager)['diff']['semantic_concepts']==[]


def test_semantic_impact_is_authorized_and_preserves_existing_values(env,tax,tmp_path):
    from media_tag.storage.filesystem import FilesystemBackend
    from media_tag.catalog.service import encode
    lex=publish(env,lexicon(tax['release_id']))
    originals=tmp_path/'originals';originals.mkdir();(originals/'synthetic.txt').write_text('Synthetic existing asset')
    snapshots=[]
    for actor in (env.manager,env.other):
        asset=env.catalog.register_file(FilesystemBackend(originals),'synthetic.txt',source_backend_id='synthetic-source',
                    principal=actor,request_id='register',name='Synthetic existing fixture')
        snapshots.append(asset)
        with env.database.transaction(write=True) as c:
            # Existing indexed human value fixture; no new medical approval is claimed.
            value={'field':'疾病/症状','value':{'state':'observed','values':['合成选项甲-仅测试']},'actor_id':actor.actor_id,'revision':1,
                   'evidence_ids':['synthetic-prior-observation'],'confirmed_at':'2026-11-08T00:00:00Z'}
            c.execute('INSERT INTO effective_values VALUES(?,?,?)',(asset.asset_id,'疾病/症状',encode(value)))
    with env.database.transaction() as c:before=[tuple(r) for r in c.execute('SELECT * FROM effective_values ORDER BY asset_id')]
    candidate=create(env,lexicon(tax['release_id'],concepts=[concept(definition='Changed synthetic meaning')]),base=lex['release_id'])
    impact=env.service.preview(candidate['candidate_id'],principal=env.manager)['diff']
    assert impact['affected_asset_ids']==[snapshots[0].asset_id]
    with env.database.transaction() as c:
        assert [tuple(r) for r in c.execute('SELECT * FROM effective_values ORDER BY asset_id')]==before
        assert all(r[0]==1 for r in c.execute('SELECT revision FROM assets'))
        assert c.execute('SELECT count(*) FROM router_jobs').fetchone()[0]==0


def test_rollback_failure_after_switch_keeps_previous_pair(env,tax,monkeypatch):
    lex=publish(env,lexicon(tax['release_id']));before=env.service.active(principal=env.owner)
    spec=Rollback(request_id='rollback-failure',expected_generation=2,taxonomy_release_id=tax['release_id'],note='Synthetic rollback disk failure')
    def fail(*args,**kwargs):raise OSError('synthetic audit write failure')
    monkeypatch.setattr(env.service,'_done',fail)
    with pytest.raises(OSError):env.service.rollback(spec,principal=env.publisher)
    assert env.service.active(principal=env.owner)==before and before['lexicon_release_id']==lex['release_id']


def test_unrelated_manifest_projection_cannot_be_consumed(env,tax):
    path=env.workspace/tax['manifest_ref'];manifest=json.loads(path.read_bytes());content=path.parent/'content.json'
    data=json.loads(content.read_bytes());data['fields']['疾病/症状']=['伪造投影']
    content.write_text(json.dumps(data),encoding='utf-8');manifest['content_sha256']=sha256(content.read_bytes()).hexdigest()
    path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError):ReviewPolicy(env.contract,ReviewDeployment(enabled=True,taxonomy_manifest=str(path),taxonomy_version=tax['version'],
                            taxonomy_manifest_sha256=sha256(path.read_bytes()).hexdigest(),trusted_publishers=(env.publisher.actor_id,)))


def test_version_name_is_immutable_and_old_rollbacks_rehash_target(env,tax):
    first=publish(env,lexicon(tax['release_id']))
    candidate=approve(env,create(env,lexicon(tax['release_id'],aliases=[]),base=first['release_id']))
    spec=publication(env,candidate)
    with pytest.raises(RevisionConflict):env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)
    assert env.service.active(principal=env.owner)['generation']==2
    second=publish(env,taxonomy(option(),option('option-b',value='合成乙')),'synthetic-v2',tax['release_id'])
    (env.workspace/first['manifest_ref']).write_bytes(b'{}')
    rollback=Rollback(request_id='corrupt-rollback',expected_generation=3,taxonomy_release_id=tax['release_id'],lexicon_release_id=first['release_id'],note='Synthetic corrupted target')
    with pytest.raises(IntegrityError):env.service.rollback(rollback,principal=env.publisher)
    assert env.service.active(principal=env.owner)['taxonomy_release_id']==second['release_id']
