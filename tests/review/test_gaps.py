import pytest
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.review.models import GapSubmission,GapRevision


def create(env,request='gap',**values):
    data={'需要的画面':{'state':'observed','values':['Synthetic requested visual']},**values}
    return env.reviews.gap_create(GapSubmission.model_validate_json(__import__('json').dumps({'request_id':request,'values':data,'note':'Synthetic gap request'})),principal=env.owner)


def test_gap_repetition_is_idempotent_and_counts_new_requests(reviewed):
    env=reviewed;one=create(env);assert create(env)==one
    two=create(env,'again');assert two['gap_id']==one['gap_id'] and two['revision']==2
    gap=env.reviews.gaps(principal=env.owner)['items'][0]
    assert gap['occurrences']==2 and gap['values']['处理状态']['values']==['待补']
    assert env.reviews.gaps(principal=env.other)['items']==[]
    with pytest.raises(AccessDenied):env.reviews.gap_revise(one['gap_id'],GapRevision(request_id='other',expected_revision=2,values={},note='Synthetic denied edit'),principal=env.other)


def test_gap_resolution_requires_authorized_material_and_cas(reviewed):
    env=reviewed;gap=create(env)
    values={'处理状态':{'state':'observed','values':('已补',)}}
    with pytest.raises(ValueError):env.reviews.gap_revise(gap['gap_id'],GapRevision(request_id='missing',expected_revision=1,values=values,note='Synthetic invalid resolution'),principal=env.owner)
    values['补上的素材']={'state':'observed','values':(env.asset.asset_id,)}
    result=env.reviews.gap_revise(gap['gap_id'],GapRevision(request_id='resolve',expected_revision=1,values=values,note='Synthetic explicit link'),principal=env.owner)
    assert result['revision']==2
    with pytest.raises(RevisionConflict):env.reviews.gap_revise(gap['gap_id'],GapRevision(request_id='stale',expected_revision=1,values=values,note='Synthetic stale edit'),principal=env.owner)
