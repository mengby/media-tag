from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from fastapi.testclient import TestClient

from media_tag.api.app import create_app
from media_tag.catalog import Catalog,Database,Principal
from media_tag.contracts.templates import load_template_bundle
from media_tag.router.service import Router
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.terminology.contracts import Deployment,CandidateSubmission,Review,Publish,Taxonomy,Lexicon
from media_tag.terminology.lexicon.service import TerminologyService

FINGERPRINT='40365931589406cfc00d5ea29fd2187b63407eaefa3f7361f50c8b7884dc53af'
SOURCE={'kind':'synthetic','locator':'Synthetic fixture row 1','quote':'Synthetic abstract symbol, not a medical statement','usage':'Engineering tests only'}


def option(eid='option-a',field='疾病/症状',value='合成选项甲-仅测试',**changes):
    return dict(entry_id=eid,field=field,value=value,sources=[SOURCE],rationale='Synthetic taxonomy fixture',**changes)


def taxonomy(*options):
    return Taxonomy.model_validate_json(json.dumps({'template_fingerprint':FINGERPRINT,'options':list(options or (option(),))}))


def concept(eid='concept-a',oid='option-a',**changes):
    return dict(entry_id=eid,name=eid+'-合成',concept_type='symptom',definition='Synthetic definition',scope='Synthetic tests',
                option_ids=[oid],sources=[SOURCE],rationale='Synthetic concept fixture')|changes


def alias(eid='alias-a',**changes):
    return dict(entry_id=eid,expression='合成说法甲',target_id='concept-a',relation='equivalent',default_expand=True,
                sources=[SOURCE],rationale='Synthetic alias fixture')|changes


def relation(eid='relation-a',**changes):
    return dict(entry_id=eid,source_id='concept-a',target_id='concept-b',relation='related',
                sources=[SOURCE],rationale='Synthetic relation fixture')|changes


def lexicon(taxid,**changes):
    return Lexicon.model_validate_json(json.dumps(dict(template_fingerprint=FINGERPRINT,taxonomy_release_id=taxid,
                  concepts=[concept()],aliases=[alias()],relations=[])|changes))


def create(env,payload,*,base=None,actor=None,request=None):
    spec=CandidateSubmission(request_id=request or 'create-'+str(__import__('uuid').uuid4()),kind='taxonomy' if isinstance(payload,Taxonomy) else 'lexicon',
                             base_release_id=base,payload=payload,note='Synthetic candidate creation')
    return env.service.submit(spec,principal=actor or env.owner)


def approve(env,candidate):
    cid=candidate['candidate_id'];current=env.service.get(cid,principal=env.manager)
    submitted=env.service.review(cid,Review(request_id='submit-'+cid,expected_revision=current['revision'],action='submit',note='Synthetic submission'),principal=env.owner)
    from media_tag.terminology.lexicon.validation import entries
    payload=(Taxonomy if current['kind']=='taxonomy' else Lexicon).model_validate_json(json.dumps(current['payload']))
    return env.service.review(cid,Review(request_id='approve-'+cid,expected_revision=submitted['revision'],action='approve',
                entry_ids=tuple(e.entry_id for _,e in entries(payload)),note='Synthetic explicit per-entry review, no medical acceptance'),principal=env.manager)


def publish(env,payload,version='synthetic-v1',base=None):
    candidate=create(env,payload,base=base);approved=approve(env,candidate)
    spec=Publish(request_id='publish-'+candidate['candidate_id'],expected_revision=approved['revision'],
                 expected_generation=env.service.active(principal=env.owner)['generation'],version=version,note='Synthetic engineering publication only')
    return env.service.publish(candidate['candidate_id'],spec,principal=env.publisher)


@pytest.fixture
def env(tmp_path):
    objects=tmp_path/'objects';objects.mkdir();workspace=tmp_path/'workspace';workspace.mkdir()
    contract=load_template_bundle(Path('asset-index-template'),expected_fingerprint=FINGERPRINT)
    catalog=Catalog(Database(tmp_path/'synthetic.sqlite3'),FilesystemBackend(objects,writable=True),contract=contract)
    router=Router(catalog,workspace,{})
    identities={name:Principal(actor_id='synthetic-'+name,role=role) for name,role in
                [('owner','editor'),('other','editor'),('manager','editor'),('publisher','editor'),('admin','admin'),('worker','worker'),('reader','reader')]}
    config=Deployment(enabled=True,managers=(identities['manager'].actor_id,),publishers=(identities['publisher'].actor_id,))
    service=TerminologyService(router,config)
    app=create_app(router,{sha256(k.encode()).hexdigest():v for k,v in identities.items()},web=True,terminology=service)
    return SimpleNamespace(**identities,service=service,router=router,catalog=catalog,database=catalog.database,contract=contract,config=config,
                workspace=workspace,app=app,clients={k:TestClient(app,headers={'Authorization':'Bearer '+k}) for k in identities})


@pytest.fixture
def tax(env):return publish(env,taxonomy())
