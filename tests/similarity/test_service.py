import pytest

from media_tag.catalog import AccessDenied, Principal, RevisionConflict
from media_tag.files.service import FileError
from media_tag.similarity.contracts import SourceVersion
from media_tag.similarity.engines import compare, visual_bytes
from media_tag.similarity.ledger import RelationDecision, RelationLedger

from .conftest import picture


def pair(a):
    left=a.register(picture(),name='left.png')
    right=a.register(picture(size=96),name='right.png')
    ld=a.service.describe(left,principal=a.env.owner,media_kind='image')
    rd=a.service.describe(right,principal=a.env.owner,media_kind='image')
    return left,right,ld,rd


def test_candidate_pool_never_discloses_inaccessible_assets_or_inherits_owner(authorized):
    a=authorized
    left,right,ld,rd=pair(a)
    other=Principal(actor_id='synthetic-other')
    secret=a.register(picture(),name='secret.png',principal=other)
    assert left.content_sha256==secret.content_sha256
    # Descriptor produced under its actual owner, not through the query actor.
    hidden=a.service.describe(secret,principal=other,media_kind='image')
    output=a.service.candidates(ld,[rd,hidden],principal=a.env.owner)
    assert len(output)==1 and output[0].right.asset_id==right.asset_id
    assert not output[0].ownership_changed
    with pytest.raises(AccessDenied):
        a.service.describe(secret,principal=a.env.owner,media_kind='image')


def test_relation_offer_decision_reopen_idempotency_preserves_catalogue(authorized):
    a=authorized
    left,right,ld,rd=pair(a)
    before=a.env.catalog.get_asset(left.asset_id,principal=a.env.owner)
    ledger=RelationLedger(a.tmp/'relations',a.service)
    offer=ledger.offer(compare(ld,rd),request_id='offer',principal=a.env.owner)
    assert offer['state']=='pending_human_relation' and offer['decision'] is None
    spec=RelationDecision(request_id='review',expected_revision=offer['revision'],relation='same_recording',note='Synthetic comparison of geometry only')
    decided=ledger.decide(offer['relation_id'],spec,principal=a.env.owner)
    assert decided['revision']==2 and decided['decision']['decided_by']==a.env.owner.actor_id
    reopened=RelationLedger(a.tmp/'relations',a.service)
    assert reopened.decide(offer['relation_id'],spec,principal=a.env.owner)['reused_request']
    assert len(reopened.list(principal=a.env.owner))==1
    assert [event['action'] for event in reopened.history(offer['relation_id'],principal=a.env.owner)]==['offer','decide']
    assert not decided['medical_confirmation'] and not decided['privacy_confirmation'] and not decided['labels_copied']
    after=a.env.catalog.get_asset(left.asset_id,principal=a.env.owner)
    assert after.revision==before.revision and after.effective_values==before.effective_values and after.owner_id==before.owner_id
    with pytest.raises(RevisionConflict):
        reopened.decide(offer['relation_id'],spec.model_copy(update={'relation':'different'}),principal=a.env.owner)
    with pytest.raises(AccessDenied):
        reopened.decide(offer['relation_id'],spec,principal=a.env.workers[0])


def test_stale_or_revoked_sources_cannot_reuse_cached_decisions(authorized):
    a=authorized
    left,right,ld,rd=pair(a)
    ledger=RelationLedger(a.tmp/'relations',a.service)
    row=ledger.offer(compare(ld,rd),request_id='offer',principal=a.env.owner)
    other=Principal(actor_id='synthetic-shared')
    for source in (left,right):
        asset=a.env.catalog.get_asset(source.asset_id,principal=a.env.owner)
        a.env.catalog.change_access(asset.asset_id,principal=a.env.owner,expected_revision=asset.revision,
            subject_id=other.actor_id,permission='read',reason='Synthetic share changes version')
    with pytest.raises(RevisionConflict):
        ledger.get(row['relation_id'],principal=a.env.owner)
    assert ledger.list(principal=a.env.owner)==[]
    with pytest.raises(RevisionConflict):
        a.service.candidates(ld,[rd],principal=a.env.owner)


def test_relation_requires_both_current_edit_permissions_for_decision(authorized):
    a=authorized
    left,right,_,_=pair(a)
    other=Principal(actor_id='synthetic-reader')
    refreshed=[]
    for source in (left,right):
        asset=a.env.catalog.get_asset(source.asset_id,principal=a.env.owner)
        a.env.catalog.change_access(asset.asset_id,principal=a.env.owner,expected_revision=asset.revision,
            subject_id=other.actor_id,permission='read',reason='Synthetic read grant')
        asset=a.env.catalog.get_asset(source.asset_id,principal=a.env.owner)
        refreshed.append(SourceVersion(asset_id=asset.asset_id,asset_revision=asset.revision,content_sha256=asset.sha256))
    descriptors=[a.service.describe(source,principal=other,media_kind='image') for source in refreshed]
    ledger=RelationLedger(a.tmp/'relations',a.service)
    row=ledger.offer(compare(*descriptors),request_id='reader-offer',principal=other)
    with pytest.raises(AccessDenied):
        ledger.decide(row['relation_id'],RelationDecision(request_id='reader-review',expected_revision=1,
            relation='related',note='Synthetic read-only actor'),principal=other)


def test_relation_decision_and_request_receipt_rollback_atomically(authorized,monkeypatch):
    a=authorized;left,right,ld,rd=pair(a)
    ledger=RelationLedger(a.tmp/'relations',a.service)
    row=ledger.offer(compare(ld,rd),request_id='offer',principal=a.env.owner)
    spec=RelationDecision(request_id='decision',expected_revision=1,relation='different',note='Synthetic rollback check')
    original=ledger._view
    def fail(record):
        if record['revision']==2:
            raise RuntimeError('synthetic response persistence failure')
        return original(record)
    monkeypatch.setattr(ledger,'_view',fail)
    with pytest.raises(RuntimeError,match='persistence'):
        ledger.decide(row['relation_id'],spec,principal=a.env.owner)
    monkeypatch.setattr(ledger,'_view',original)
    assert ledger.get(row['relation_id'],principal=a.env.owner)['revision']==1
    assert ledger.decide(row['relation_id'],spec,principal=a.env.owner)['revision']==2


def test_current_file_gate_is_rechecked_before_candidates_or_cached_relations(authorized,monkeypatch):
    a=authorized;left,right,ld,rd=pair(a)
    ledger=RelationLedger(a.tmp/'relations',a.service)
    row=ledger.offer(compare(ld,rd),request_id='offer',principal=a.env.owner)
    original=a.service.files._gate
    monkeypatch.setattr(a.service.files,'_gate',lambda connection,asset,**kwargs:
        'privacy_review_pending' if asset['asset_id']==right.asset_id else original(connection,asset,**kwargs))
    assert a.service.candidates(ld,[rd],principal=a.env.owner)==[]
    with pytest.raises(FileError):
        ledger.get(row['relation_id'],principal=a.env.owner)
    assert ledger.list(principal=a.env.owner)==[]


def test_foreign_database_is_rejected_without_changing_its_contents(authorized):
    import sqlite3
    from hashlib import sha256
    a=authorized
    directory=a.tmp/'foreign';directory.mkdir();(directory/'README.md').write_text('Synthetic unrelated database.\n')
    path=directory/'relations.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE unrelated(value TEXT)')
        connection.execute("INSERT INTO unrelated VALUES('synthetic untouched')")
    before=sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError,match='foreign'):
        RelationLedger(directory,a.service)
    assert sha256(path.read_bytes()).hexdigest()==before


@pytest.mark.parametrize('legacy_version',['ct-visual-dhash-alpha-v1','ct-audio-logbands-v1'])
def test_legacy_v1_candidates_remain_unqualified_for_current_review(authorized,legacy_version):
    from media_tag.similarity.contracts import Candidate
    import json
    a=authorized;left,right,ld,rd=pair(a)
    payload=compare(ld,rd).model_dump(mode='json');payload['algorithm_version']=legacy_version
    legacy=Candidate.model_validate_json(json.dumps(payload))
    ledger=RelationLedger(a.tmp/'relations',a.service)
    with pytest.raises(RevisionConflict,match='profile changed'):
        ledger.offer(legacy,request_id='legacy-offer',principal=a.env.owner)
    assert ledger.list(principal=a.env.owner)==[]
