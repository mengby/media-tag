from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from io import BytesIO
import json
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from media_tag.api.app import create_app
from media_tag.catalog import Catalog,Database,Principal
from media_tag.contracts.templates import load_template_bundle
from media_tag.contracts.models import FieldValue
from media_tag.router.service import Router
from media_tag.router.runtime import cpu_stages,CPUHandlers,LocalExecutor
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.upload.service import UploadService
from media_tag.upload.models import UploadSpec
from media_tag.review.service import ReviewService
from media_tag.review.models import ReviewDeployment,Observation,FieldDecision
from media_tag.export.service import ExportService
from upload.conftest import tools

FINGERPRINT='40365931589406cfc00d5ea29fd2187b63407eaefa3f7361f50c8b7884dc53af'


@pytest.fixture
def reviewed(tmp_path,tools):
    folders={}
    for name in ('originals','objects','workspace'):
        folder=tmp_path/name;folder.mkdir();(folder/'README.md').write_text('Synthetic D06 test only.\n');folders[name]=folder
    contract=load_template_bundle(Path('asset-index-template'),expected_fingerprint=FINGERPRINT)
    catalog=Catalog(Database(tmp_path/'synthetic.sqlite3'),FilesystemBackend(folders['objects'],writable=True),contract=contract)
    owner=Principal(actor_id='synthetic-owner');reviewer=Principal(actor_id='synthetic-reviewer');other=Principal(actor_id='synthetic-other')
    reader=Principal(actor_id='synthetic-reader',role='reader');worker=Principal(actor_id='synthetic-worker',role='worker');admin=Principal(actor_id='synthetic-admin',role='admin')
    router=Router(catalog,folders['workspace'],cpu_stages(tools));router.register_worker(worker.actor_id,tuple(router.stages),principal=admin)
    uploads=UploadService(router,tools)
    image=BytesIO();Image.new('RGBA',(32,24),(255,0,0,100)).save(image,format='PNG');data=image.getvalue()
    (folders['originals']/'synthetic.png').write_bytes(data)
    receipt=uploads.create(UploadSpec(request_id='synthetic-source',filename='synthetic.png',size_bytes=len(data),sha256=sha256(data).hexdigest()),principal=owner)
    uploads.append(receipt['upload_id'],0,data,sha256(data).hexdigest(),principal=owner)
    done=uploads.complete(receipt['upload_id'],principal=owner)
    executor=LocalExecutor(router,worker,CPUHandlers(router,worker,tools))
    assert executor.run_once()['accepted'] and executor.run_once()['accepted']
    asset=catalog.get_asset(done['asset_id'],principal=owner)
    catalog.change_access(asset.asset_id,principal=owner,expected_revision=asset.revision,subject_id=reviewer.actor_id,permission='edit',reason='Synthetic explicit medical review grant')
    reviews=ReviewService(router,ReviewDeployment(enabled=True,reviewers=(reviewer.actor_id,)))
    identities={key:value for key,value in [('owner',owner),('reviewer',reviewer),('other',other),('reader',reader),('worker',worker)]}
    app=create_app(router,{sha256(k.encode()).hexdigest():v for k,v in identities.items()},uploads=uploads,web=True,reviews=reviews)
    return SimpleNamespace(**folders,catalog=catalog,database=catalog.database,contract=contract,owner=owner,reviewer=reviewer,
        other=other,reader=reader,worker=worker,admin=admin,router=router,uploads=uploads,tools=tools,asset=asset,
        reviews=reviews,exports=app.state.exports,app=app,data=data,executor=executor,
        clients=SimpleNamespace(**{k:TestClient(app,headers={'Authorization':'Bearer '+k}) for k in identities}))


def current(env,actor=None):return env.catalog.get_asset(env.asset.asset_id,principal=actor or env.owner)


def observation(env,*,actor=None,kind='image',**kwargs):
    actor=actor or env.owner;asset=current(env,actor)
    spec=Observation(request_id='obs-'+str(len(env.reviews.detail(asset.asset_id,principal=actor)['observations'])),expected_revision=asset.revision,
        source_sha256=asset.sha256,kind=kind,note='Synthetic observation, not a production medical finding',viewed=kind!='human_record',**kwargs)
    return env.reviews.observe(asset.asset_id,spec,principal=actor)['observation_id']


def decide(env,field,*,actor=None,values=None,state='observed',obs=None,request=None,**kwargs):
    actor=actor or env.owner;asset=current(env,actor)
    value=FieldValue(state=state,values=tuple(values) if values is not None else None,reason=None if state=='observed' else 'Synthetic explicit state')
    spec=FieldDecision(request_id=request or 'field-'+str(asset.revision),expected_revision=asset.revision,source_sha256=asset.sha256,
        field=field,value=value,observation_ids=(obs or observation(env,actor=actor),),note='Synthetic single-field decision',**kwargs)
    return env.reviews.decide(asset.asset_id,spec,principal=actor)
