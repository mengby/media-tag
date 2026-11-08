import json
import pytest
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.terminology.contracts import CandidateSubmission,CandidateRevision,Review,Publish,ImportRows
from media_tag.terminology.lexicon.service import PublicationBlocked
from .conftest import create,approve,publish,taxonomy,lexicon,option,concept,alias,SOURCE


def test_unreviewed_cannot_publish_and_approval_is_not_activation(env):
    candidate=create(env,taxonomy());cid=candidate['candidate_id']
    with pytest.raises(PublicationBlocked):env.service.publish(cid,Publish(request_id='early',expected_revision=1,expected_generation=0,version='synthetic-v1',note='Synthetic attempt'),principal=env.publisher)
    approved=approve(env,candidate)
    assert approved['state']=='approved'
    assert env.service.active(principal=env.owner)['generation']==0
    assert env.service.get(cid,principal=env.manager)['approvals']['option-a']['actor_id']==env.manager.actor_id


@pytest.mark.parametrize('actor',['owner','other','admin','worker','reader'])
def test_explicit_publication_role_required_even_for_admin(env,actor):
    candidate=create(env,taxonomy());approved=approve(env,candidate)
    with pytest.raises(AccessDenied):env.service.publish(candidate['candidate_id'],Publish(request_id='denied',expected_revision=approved['revision'],expected_generation=0,
                                      version='synthetic-v1',note='Synthetic denial'),principal=getattr(env,actor))
    assert env.service.active(principal=env.owner)['generation']==0


def test_owner_scope_revision_conflict_and_idempotency(env):
    spec=CandidateSubmission(request_id='same',kind='taxonomy',payload=taxonomy(),note='Synthetic request')
    first=env.service.submit(spec,principal=env.owner)
    assert env.service.submit(spec,principal=env.owner)==first
    with pytest.raises(RevisionConflict):env.service.submit(spec.model_copy(update={'note':'Changed'}),principal=env.owner)
    with pytest.raises(AccessDenied):env.service.get(first['candidate_id'],principal=env.other)
    assert env.service.list_candidates(principal=env.other)['items']==[]
    approved=approve(env,first)
    with pytest.raises(RevisionConflict):env.service.revise(first['candidate_id'],CandidateRevision(request_id='stale',expected_revision=1,payload=taxonomy(),note='Synthetic stale edit'),principal=env.owner)
    result=env.service.revise(first['candidate_id'],CandidateRevision(request_id='edit',expected_revision=approved['revision'],payload=taxonomy(),note='Synthetic edit resets all approvals'),principal=env.owner)
    assert result['state']=='draft' and env.service.get(first['candidate_id'],principal=env.owner)['approvals']=={}


def test_unknown_target_and_missing_source_remain_drafts(env,tax):
    candidate=create(env,lexicon(tax['release_id'],aliases=[alias(target_id=None,sources=[])]))
    detail=env.service.get(candidate['candidate_id'],principal=env.owner)
    assert {p['code'] for p in detail['issues']} >= {'missing_source','missing_active_target'}
    env.service.review(candidate['candidate_id'],Review(request_id='s',expected_revision=1,action='submit',note='Synthetic submission'),principal=env.owner)
    with pytest.raises(PublicationBlocked):env.service.review(candidate['candidate_id'],Review(request_id='a',expected_revision=2,action='approve',entry_ids=('alias-a',),note='Synthetic invalid approval'),principal=env.manager)


def test_import_report_atomic_bad_rows_duplicate_source_merge_and_ambiguity(env,tax):
    candidate=create(env,lexicon(tax['release_id']));cid=candidate['candidate_id']
    rows=(alias('alias-b',sources=[SOURCE|{'locator':'Synthetic row 2'}]),{'bad':'not silently dropped'})
    spec=ImportRows(request_id='bad-batch',expected_revision=1,entity='aliases',rows=rows,apply=True,note='Synthetic mixed batch')
    report=env.service.import_rows(cid,spec,principal=env.owner)
    assert [r['status'] for r in report['rows']]==['merged','invalid'] and not report['applied']
    assert env.service.get(cid,principal=env.owner)['revision']==1
    preview=env.service.import_rows(cid,spec.model_copy(update={'request_id':'preview','rows':rows[:1],'apply':False}),principal=env.owner)
    assert not preview['applied']
    accepted=env.service.import_rows(cid,spec.model_copy(update={'request_id':'apply','rows':rows[:1]}),principal=env.owner)
    assert accepted['applied'] and accepted['revision']==2
    detail=env.service.get(cid,principal=env.owner)
    assert len(detail['payload']['aliases'])==1 and len(detail['payload']['aliases'][0]['sources'])==2
    assert env.service.import_rows(cid,spec.model_copy(update={'request_id':'apply','rows':rows[:1]}),principal=env.owner)==accepted
    assert env.service.get(cid,principal=env.owner)['state']=='draft'


def test_per_entry_review_reject_and_no_model_queue_side_effects(env,tax):
    candidate=create(env,lexicon(tax['release_id']));cid=candidate['candidate_id']
    env.service.review(cid,Review(request_id='s',expected_revision=1,action='submit',note='Synthetic submission'),principal=env.owner)
    first=env.service.review(cid,Review(request_id='a',expected_revision=2,action='approve',entry_ids=('concept-a',),note='Synthetic concept approval'),principal=env.manager)
    assert first['state']=='review' and first['revision']==3
    denied=env.service.review(cid,Review(request_id='r',expected_revision=3,action='reject',note='Synthetic needs more evidence'),principal=env.manager)
    assert denied['state']=='rejected'
    with env.database.transaction() as c:
        assert c.execute('SELECT count(*) FROM router_jobs').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM effective_values').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM index_confirmations').fetchone()[0]==0


def test_import_existing_id_cannot_hide_behind_semantic_duplicate(env,tax):
    payload=lexicon(tax['release_id'],aliases=[alias(),alias('alias-b',expression='另一合成说法')])
    candidate=create(env,payload)
    report=env.service.import_rows(candidate['candidate_id'],ImportRows(request_id='collision',expected_revision=1,entity='aliases',
        rows=(alias('alias-b'),),apply=True,note='Synthetic id collision'),principal=env.owner)
    assert not report['applied'] and report['rows'][0]['status']=='conflict'
    assert env.service.get(candidate['candidate_id'],principal=env.owner)['payload']==payload.model_dump(mode='json')


def test_removed_manager_approval_requires_new_review(env):
    from media_tag.terminology.contracts import Deployment
    from media_tag.terminology.lexicon.service import TerminologyService
    candidate=approve(env,create(env,taxonomy()))
    service=TerminologyService(env.router,Deployment(enabled=True,publishers=(env.publisher.actor_id,)))
    with pytest.raises(PublicationBlocked) as error:
        service.publish(candidate['candidate_id'],Publish(request_id='removed-approval',expected_revision=candidate['revision'],expected_generation=0,
                            version='synthetic-v1',note='Synthetic removed capability'),principal=env.publisher)
    assert error.value.problems==[{'code':'missing_current_approval','entry_id':'option-a'}]
