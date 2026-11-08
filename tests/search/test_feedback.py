import pytest
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.search.models import Feedback,FeedbackRevision,FeedbackDecision


def test_unknown_target_duplicate_sources_and_no_automatic_release(sought):
    env=sought.env;service=sought.search.feedback
    spec=Feedback(request_id='feedback',expression='不确定的合成说法',quote='I do not know its professional name')
    first=service.create(spec,principal=env.owner)
    assert service.create(spec,principal=env.owner)==first
    second=service.create(spec.model_copy(update={'request_id':'second','quote':'Another actual input context'}),principal=env.owner)
    assert second['feedback_id']==first['feedback_id'] and second['occurrences']==2
    detail=service.get(first['feedback_id'],principal=env.owner)
    assert len(detail['events'])==2 and detail['payload']['target_id'] is None
    assert service.listing(principal=env.other)['items']==[]
    with pytest.raises(AccessDenied):service.get(first['feedback_id'],principal=env.other)
    with pytest.raises(AccessDenied):service.decide(first['feedback_id'],FeedbackDecision(request_id='denied',expected_revision=second['revision'],action='promote',note='Synthetic denial'),principal=env.owner)
    before=env.service.active(principal=env.owner)
    promoted=service.decide(first['feedback_id'],FeedbackDecision(request_id='promote',expected_revision=second['revision'],action='promote',note='Synthetic draft only'),principal=env.manager)
    assert promoted['state']=='draft_created' and not promoted['published']
    candidate=env.service.get(promoted['candidate_id'],principal=env.manager)
    assert candidate['state']=='draft' and any(p['code']=='missing_active_target' for p in candidate['issues'])
    assert env.service.active(principal=env.owner)==before


def test_owner_revision_withdrawal_and_manager_needs_info_use_cas(sought):
    env=sought.env;service=sought.search.feedback
    first=service.create(Feedback(request_id='new',expression='合成补充'),principal=env.owner)
    changed=service.decide(first['feedback_id'],FeedbackDecision(request_id='info',expected_revision=1,action='needs_info',note='Need actual source context'),principal=env.manager)
    with pytest.raises(RevisionConflict):service.revise(first['feedback_id'],FeedbackRevision(request_id='stale',expression='合成补充',expected_revision=1),principal=env.owner)
    withdrawn=service.revise(first['feedback_id'],FeedbackRevision(request_id='withdraw',expression='合成补充',expected_revision=changed['revision'],withdraw=True),principal=env.owner)
    assert withdrawn['state']=='withdrawn'
    with pytest.raises(RevisionConflict):service.decide(first['feedback_id'],FeedbackDecision(request_id='late',expected_revision=withdrawn['revision'],action='promote',note='Synthetic invalid action'),principal=env.manager)


def test_incorrect_mapping_preserves_reported_version_without_preselecting_target(sought):
    service=sought.search.feedback;env=sought.env
    spec=Feedback(request_id='wrong',expression='合成圆圈',kind='incorrect_mapping',reported_target_id='concept-a',
        reported_entry_id='alias-a',reported_lexicon_release_id=sought.lex['release_id'])
    item=service.create(spec,principal=env.owner)
    payload=service.get(item['feedback_id'],principal=env.owner)['payload']
    assert payload['target_id'] is None and payload['reported_target_id']=='concept-a'
    assert payload['reported_lexicon_release_id']==sought.lex['release_id']
    with pytest.raises(ValueError):service.create(spec.model_copy(update={'request_id':'invalid','reported_entry_id':'missing'}),principal=env.owner)
