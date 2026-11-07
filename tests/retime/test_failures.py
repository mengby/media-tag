import json
import pytest
from media_tag.contracts.models import Versions,WorkerResult,TaskLimits
from media_tag.router.models import JobSpec,StepSpec
from media_tag.transforms.models import RetimeSubmission,VideoOptions,RETIME_VERSION
from media_tag.variants.models import Recipe,PlanSpec,MaskSpec


def plan(env,**kwargs):
    return env.retimes.variants.create_plan(PlanSpec(parent_asset_id=env.parent.asset_id,expected_revision=1,
        recipe=Recipe(renderer_revision=RETIME_VERSION,**kwargs)),request_id='synthetic-plan',principal=env.owner)


@pytest.mark.parametrize('fault',['gpu','model','version','target'])
def test_deterministic_stage_rejects_unbound_resources_models_and_fields(retiming,fault):
    env=retiming;planned=plan(env)
    config=VideoOptions().model_dump()|{'variant_plan_id':planned['plan_id'],'variant_plan_hash':planned['recipe_hash']}
    versions=Versions(rules_version=RETIME_VERSION,preprocess_version='wrong' if fault=='version' else RETIME_VERSION,
        model_revision='not-a-model-stage' if fault=='model' else None)
    step=StepSpec(key='retime',stage='retime_render',config=config,versions=versions,
        limits=TaskLimits(gpu_slots=int(fault=='gpu')),target_fields=('medical',) if fault=='target' else ())
    with pytest.raises(ValueError):env.router.submit(JobSpec(asset_id=env.parent.asset_id,steps=(step,)),request_id='invalid',principal=env.owner)


def test_generic_job_cannot_claim_to_apply_a_mask_in_the_plain_retime_renderer(retiming):
    env=retiming;planned=plan(env,mask=MaskSpec(revision='synthetic-nonexistent-mask'))
    config=VideoOptions().model_dump()|{'variant_plan_id':planned['plan_id'],'variant_plan_hash':planned['recipe_hash']}
    job=env.router.submit(JobSpec(asset_id=env.parent.asset_id,steps=(StepSpec(key='retime',stage='retime_render',config=config,
        versions=Versions(rules_version=RETIME_VERSION,preprocess_version=RETIME_VERSION)),)),request_id='fake-masked-producer',principal=env.owner)
    assert env.executor.run_once()['accepted']
    with env.database.transaction() as connection:
        result=WorkerResult.model_validate_json(connection.execute('SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=?',(job['job_id'],)).fetchone()[0])
        assert not connection.execute('SELECT 1 FROM variants').fetchone()
    assert result.status=='failed' and result.errors[0].code=='mask_requires_redaction_renderer'


def test_budget_failure_and_cancel_do_not_publish_or_fall_back_to_the_source(retiming):
    env=retiming
    job=env.retimes.start(RetimeSubmission(request_id='budget',parent_asset_id=env.parent.asset_id,expected_revision=1,
        options=VideoOptions(max_seconds=.5)),principal=env.owner)
    assert env.executor.run_once()['accepted']
    status=env.retimes.get(job['job_id'],principal=env.owner)
    assert status['asset_id'] is None and status['job']['state']=='needs_attention'
    assert status['job']['tasks'][0]['error_code']=='duration_budget'
    second=env.retimes.start(RetimeSubmission(request_id='cancel',parent_asset_id=env.parent.asset_id,expected_revision=1),principal=env.owner)
    env.router.cancel(second['job_id'],principal=env.owner)
    assert env.executor.run_once() is None
    assert env.retimes.get(second['job_id'],principal=env.owner)['asset_id'] is None
