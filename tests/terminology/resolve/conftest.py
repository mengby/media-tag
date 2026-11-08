from hashlib import sha256
import json
from types import SimpleNamespace
import pytest
from media_tag.api.app import create_app
from media_tag.models.contracts import ModelFailure
from media_tag.terminology.resolve.models import ResolveOptions,IndexActivation
from media_tag.terminology.resolve.service import Resolver
from ..lexicon.conftest import env,option,taxonomy,concept,alias,lexicon,publish


class FakeEncoder:
    """Protocol branch fixture, not inference. Unit axes are deliberate synthetic data."""
    def __init__(self):self.calls=[];self.fail=None;self.stats={'diagnostic_fake':True};self.on_encode=None
    def encode(self,texts,*,query=False,timeout_seconds=8.0,stopped=None):
        self.calls.append({'texts':list(texts),'query':query,'timeout':timeout_seconds})
        if self.on_encode:self.on_encode()
        if stopped and stopped.is_set():raise ModelFailure('cancelled')
        if self.fail:raise ModelFailure(self.fail)
        result=[]
        for text in texts:
            index=2 if '不相关' in text else 1 if '方形' in text else 0
            vector=[0.0]*512;vector[index]=1.0;result.append(vector)
        return result
    def close(self):pass


@pytest.fixture
def resolved(env):
    tax=publish(env,taxonomy(option(),option('option-b',value='合成乙'),option('organ',field='器官/系统',value='合成结构'),option('department',field='科室',value='合成科室')))
    payload=lexicon(tax['release_id'],concepts=[concept(name='合成圆形'),concept('concept-b',oid='option-b',name='合成方形')],aliases=[
        alias(expression='合成圆圈'),alias('alias-b',expression='方块说法',target_id='concept-b'),
        alias('poly-a',expression='多义合成词',default_expand=False,ambiguity_group='poly'),
        alias('poly-b',expression='多义合成词',target_id='concept-b',default_expand=False,ambiguity_group='poly'),
        alias('context',expression='局部说法',context={'organs':['organ']}),
        alias('related',expression='相关说法',relation='related',default_expand=False),
        alias('metaphor',expression='像圆球一样',relation='metaphor',default_expand=False)])
    lex=publish(env,payload)
    encoder=FakeEncoder();options=ResolveOptions(enabled=True,model_enabled=True)
    resolver=Resolver(env.service,options,encoder=encoder)
    activation=IndexActivation(request_id='activate-initial',expected_generation=2,expected_serving_epoch=0,
        taxonomy_release_id=tax['release_id'],lexicon_release_id=lex['release_id'],note='Synthetic text index activation')
    result=resolver.indices.activate(activation,principal=env.publisher)
    app=create_app(env.router,{sha256(name.encode()).hexdigest():getattr(env,name) for name in ('owner','manager','publisher','other','reader','worker','admin')},
        web=True,terminology=env.service,resolver=resolver)
    return SimpleNamespace(env=env,tax=tax,lex=lex,payload=payload,encoder=encoder,resolver=resolver,activation=activation,index=result,app=app)
