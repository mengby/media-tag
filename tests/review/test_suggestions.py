from hashlib import sha256
from uuid import uuid4
import pytest
from media_tag.contracts.models import ContractModel,Versions,WorkerResult
from media_tag.router.models import StageDefinition,StepSpec,JobSpec
from media_tag.workers.evidence import _directory
from media_tag.review.models import FieldDecision
from .conftest import current,observation


def machine_suggestion(env,field='画面主体',value='Synthetic candidate',kind='image'):
    class Empty(ContractModel):pass
    env.router.stages['synthetic_review']=StageDefinition(Empty,'synthetic-d06-v1',synthetic=True)
    env.router.register_worker(env.worker.actor_id,tuple(env.router.stages),principal=env.admin)
    job=env.router.submit(JobSpec(asset_id=env.asset.asset_id,steps=(StepSpec(key='synthetic',stage='synthetic_review',target_fields=(field,),
        versions=Versions(rules_version='synthetic-'+uuid4().hex,preprocess_version='synthetic-v1')),)),request_id='suggest-'+uuid4().hex,principal=env.owner)
    lease=env.router.claim(principal=env.worker);assert lease
    output=_directory(env.workspace,lease.task.output_prefix.rstrip('/'));path=output/'synthetic.png';path.write_bytes(env.data)
    result=WorkerResult.model_validate_json(__import__('json').dumps({'task_id':lease.task.task_id,'attempt':lease.task.attempt,'lease_epoch':lease.task.lease_epoch,
        'input_fingerprint':lease.task.input_fingerprint,'versions':lease.task.versions.model_dump(),'status':'succeeded','elapsed_seconds':.1,
        'artifacts':[{'kind':'image','ref':path.relative_to(env.workspace).as_posix(),'sha256':sha256(env.data).hexdigest()}],
        'evidence':[{'evidence_id':'synthetic-e','kind':kind,'source_sha256':env.asset.sha256,
            'artifact':{'kind':'image','ref':path.relative_to(env.workspace).as_posix(),'sha256':sha256(env.data).hexdigest()}}],
        'suggestions':[{'suggestion_id':'synthetic-'+uuid4().hex,'field':field,'origin':'ai','producer_version':'synthetic-protocol-not-model-inference',
            'value':{'state':'observed','values':[value]},'evidence_ids':['synthetic-e']}]}))
    accepted=env.router.complete(result,principal=env.worker);env.router.release(lease.task.task_id,lease.task.lease_epoch,principal=env.worker)
    return accepted,path,job


def test_accept_reject_exact_run_human_protection_and_actual_evidence_http(reviewed):
    env=reviewed;accepted,path,job=machine_suggestion(env)
    assert accepted['accepted']
    detail=env.reviews.detail(env.asset.asset_id,principal=env.owner);suggestion=detail['suggestions'][0]
    evidence=suggestion['evidence'][0]
    response=env.clients.owner.get(evidence['content'])
    assert response.status_code==200 and response.content==env.data
    assert env.clients.other.get(evidence['content']).status_code==403
    obs=observation(env);asset=current(env)
    spec=FieldDecision(request_id='accept',expected_revision=asset.revision,source_sha256=asset.sha256,field='画面主体',decision='accept',
        run_id=suggestion['run_id'],suggestion_id=suggestion['suggestion_id'],observation_ids=(obs,),note='Synthetic explicit adoption')
    env.reviews.decide(asset.asset_id,spec,principal=env.owner)
    assert current(env).effective_values[0].value.values==('Synthetic candidate',)
    accepted,_,_=machine_suggestion(env,value='Synthetic newer suggestion')
    assert accepted['accepted'] and current(env).effective_values[0].value.values==('Synthetic candidate',)
    newest=env.reviews.detail(asset.asset_id,principal=env.owner)['suggestions'][-1];asset=current(env)
    env.reviews.decide(asset.asset_id,FieldDecision(request_id='reject',expected_revision=asset.revision,source_sha256=asset.sha256,field='画面主体',
        decision='reject',run_id=newest['run_id'],suggestion_id=newest['suggestion_id'],note='Synthetic rejection'),principal=env.owner)
    assert current(env).effective_values[0].value.values==('Synthetic candidate',)
    path.write_bytes(b'changed')
    assert env.clients.owner.get(evidence['content']).status_code==422


def test_only_declared_choices_are_ingested_and_video_direction_not_from_frame(reviewed):
    env=reviewed
    accepted,_,_=machine_suggestion(env,field='用途大类',value='信息图解');assert accepted['accepted']
    for field,value,kind in [('用途大类','invented','image'),('科室','五官科','image'),('过程/状态','肿胀→消肿','image'),('过程/状态','肿胀→消肿','filename'),('含人脸','已处理副本','image')]:
        accepted,_,_=machine_suggestion(env,field=field,value=value,kind=kind)
        assert not accepted['accepted']
