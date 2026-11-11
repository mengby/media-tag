import json
import pytest
from search.conftest import sought,env
from media_tag.contracts.models import Versions
from media_tag.temporal_topics.normalizer import assess
from media_tag.review.models import ReviewDeployment
from media_tag.review.policy import ReviewPolicy


def parse(sought,raw,field='画面主体',policy=None):
    return assess(raw,field,policy=policy or sought.reviews.policy,
        versions=Versions(rules_version='test',preprocess_version='test'),evidence_id='continuous-segment')


def response(field,values,**extra):
    return json.dumps({'field':field,'state':'observed','values':values,'dynamic':False,**extra},ensure_ascii=False)


@pytest.mark.parametrize('raw', ['{}','```json\n{}\n```','{"field":"画面主体","field":"科室"}',
    response('科室',['x']),response('画面主体',['x'],confidence=1),response('画面主体',[]),
    response('画面主体',['x','x']),response('画面主体',[1]),response('画面主体',['x'],dynamic=1),
    response('画面主体',['x'],state='unknown')])
def test_strict_protocol_has_no_fabricated_observation(sought,raw):
    result=parse(sought,raw)
    assert result.value.state=='failed' and result.next_step=='precision'


def test_published_medical_is_only_ai_suggestion(sought):
    result=parse(sought,response('科室',['合成科室-仅测试']),'科室')
    assert result.value.state=='observed' and result.next_step=='human' and result.requires_field_review
    assert result.origin=='local'


def test_unpublished_medical_keeps_candidate(sought):
    policy=ReviewPolicy(sought.env.contract,ReviewDeployment())
    result=parse(sought,response('科室',['合成科室-仅测试']),'科室',policy)
    assert result.value.state=='no_match' and result.next_step=='vocabulary'
    assert result.candidates==('合成科室-仅测试',)


@pytest.mark.parametrize('field,values', [('情绪/语气',['急促男声']),('英文检索词',['background music']),
                                        ('中文检索词',['有旁白讲解'])])
def test_auditory_claims_need_audio_evidence(sought,field,values):
    result=parse(sought,response(field,values),field)
    assert result.value.state=='unknown' and result.reason=='audio_not_analyzed' and result.next_step=='evidence'
