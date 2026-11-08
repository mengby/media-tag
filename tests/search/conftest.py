from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from PIL import Image,ImageDraw
from media_tag.api.app import create_app
from media_tag.review.models import ReviewDeployment,Observation,FieldDecision
from media_tag.review.policy import ReviewPolicy
from media_tag.review.service import ReviewService
from media_tag.contracts.models import FieldValue
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.search.service import SearchService
from media_tag.terminology.resolve.models import ResolveOptions,IndexActivation
from media_tag.terminology.resolve.service import Resolver
from media_tag.web.service import LibraryService
from media_tag.web.models import Enrollment
from terminology.lexicon.conftest import env,publish,taxonomy,option,concept,alias,lexicon
from terminology.resolve.conftest import FakeEncoder


@pytest.fixture
def sought(env,tmp_path):
    policy=ReviewPolicy(env.contract,ReviewDeployment())
    purposes=policy.options(policy.asset_fields['用途大类'])
    assert purposes
    purpose='信息图解'
    assert purpose in purposes
    payload=taxonomy(option('purpose',field='用途大类',value=purpose),option('medical',value='合成分类-仅测试'),
        option('department',field='科室',value='合成科室-仅测试'))
    tax=publish(env,payload)
    word=lexicon(tax['release_id'],concepts=[concept(name='合成圆形',concept_type='other',oid='purpose'),
        concept('square',name='合成方形',concept_type='other',oid='purpose'),concept('clinical',name='合成医学概念',oid='medical')],
        aliases=[alias(expression='合成圆圈'),alias('square-alias',expression='方块说法',target_id='square'),
                 alias('unknown-a',expression='多义说法',default_expand=False,ambiguity_group='same'),
                 alias('unknown-b',expression='多义说法',target_id='square',default_expand=False,ambiguity_group='same')])
    lex=publish(env,word)
    encoder=FakeEncoder();resolver=Resolver(env.service,ResolveOptions(enabled=True,model_enabled=True),encoder=encoder)
    activation=resolver.indices.activate(IndexActivation(request_id='activate',expected_generation=2,expected_serving_epoch=0,
        taxonomy_release_id=tax['release_id'],lexicon_release_id=lex['release_id'],note='Synthetic text tuple'),principal=env.publisher)
    path=env.workspace/tax['manifest_ref']
    reviews=ReviewService(env.router,ReviewDeployment(enabled=True,reviewers=(env.owner.actor_id,),taxonomy_manifest=str(path),taxonomy_version=tax['version'],
        taxonomy_manifest_sha256=tax['manifest_sha256'],trusted_publishers=(env.publisher.actor_id,)))
    search=SearchService(resolver,reviews=reviews)
    originals=tmp_path/'originals';originals.mkdir();(originals/'README.md').write_text('Synthetic D15 original images, read only.')
    image=Image.new('RGB',(24,24),'white');ImageDraw.Draw(image).ellipse((3,3,20,20),fill='red');image.save(originals/'synthetic.png')
    asset=env.catalog.register_file(FilesystemBackend(originals),'synthetic.png',source_backend_id='synthetic',principal=env.owner,request_id='source',name='Unrelated original name')
    observation=reviews.observe(asset.asset_id,Observation(request_id='view',expected_revision=asset.revision,source_sha256=asset.sha256,
        kind='image',viewed=True,note='Synthetic real image observation only'),principal=env.owner)['observation_id']
    reviews.decide(asset.asset_id,FieldDecision(request_id='subject',expected_revision=asset.revision,source_sha256=asset.sha256,field='画面主体',
        value=FieldValue(state='observed',values=('合成圆形',)),observation_ids=(observation,),note='Synthetic observed geometry field'),principal=env.owner)
    asset=env.catalog.get_asset(asset.asset_id,principal=env.owner)
    library=LibraryService(env.router,people=(env.reader.actor_id,))
    library.enroll(asset.asset_id,Enrollment(request_id='enroll',expected_revision=asset.revision,output_sha256=asset.sha256,active=True,recipients=(env.reader.actor_id,)),principal=env.owner)
    asset=env.catalog.get_asset(asset.asset_id,principal=env.owner)
    app=create_app(env.router,{sha256(n.encode()).hexdigest():getattr(env,n) for n in ('owner','other','reader','manager','publisher','worker','admin')},
        web=True,reviews=reviews,terminology=env.service,resolver=resolver,search=search)
    return SimpleNamespace(env=env,search=search,resolver=resolver,encoder=encoder,reviews=reviews,asset=asset,observation=observation,
        original=originals/'synthetic.png',tax=tax,lex=lex,payload=word,activation=activation,app=app,purpose=purpose)
