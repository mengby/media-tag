from hashlib import sha256
import json
from types import SimpleNamespace

import pytest

from upload.conftest import intake, tools
from router.conftest import env
from media_tag.contracts.models import ArtifactRef, ContractModel, Identifier, Sha256, WorkerResult, Versions
from media_tag.router.models import JobSpec, StageDefinition, StepSpec
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.variants.models import PlanSpec, Recipe
from media_tag.variants.service import VariantService
from media_tag.workers.evidence import _directory


class OutputConfig(ContractModel):
    variant_plan_id: Identifier
    variant_plan_hash: Sha256


@pytest.fixture
def variants(intake, tools):
    env = intake
    path = env.originals / 'synthetic-video.mp4'
    tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=80x48:rate=10:duration=1.6', '-c:v', 'libx264', '-y', str(path)], timeout=20)
    env.parent = env.catalog.register_file(FilesystemBackend(env.originals), path.name, source_backend_id='synthetic-video', principal=env.owner, request_id='synthetic-video', name='Synthetic source video')
    env.parent_path = path
    env.variants = VariantService(env.router, tools, output_stages=('synthetic-render',))
    env.router.stages['synthetic-render'] = StageDefinition(OutputConfig, 'synthetic-output-v1', metadata_sensitive=False, synthetic=True)
    env.router.register_worker(env.workers[0].actor_id, ('synthetic-render',), principal=env.admin)
    return env


def plan(env, *, request='plan', recipe=None, parent=None, chain=False, principal=None):
    parent = parent or env.parent
    recipe = recipe or Recipe()
    if recipe.renderer_revision is None:
        recipe = Recipe.model_validate_json(json.dumps(recipe.model_dump() | {'renderer_revision': 'synthetic-output-v1'}))
    return env.variants.create_plan(PlanSpec(parent_asset_id=parent.asset_id, expected_revision=parent.revision,
        recipe=recipe, chain_from_derived=chain), request_id=request, principal=principal or env.owner)


def produced(env, planned, *, duration=None, extra_video=False):
    config = {'variant_plan_id': planned['plan_id'], 'variant_plan_hash': planned['recipe_hash']}
    job = env.router.submit(JobSpec(asset_id=planned['parent_asset_id'], steps=(StepSpec(key='render', stage='synthetic-render', config=config,
        versions=Versions(rules_version='synthetic-v1', preprocess_version=planned['contract']['spec']['recipe']['renderer_revision'],
                          model_revision=planned['contract']['spec']['recipe']['model_revision'])),)), request_id='render-' + planned['plan_id'], principal=env.owner)
    lease = env.router.claim(principal=env.workers[0]); task = lease.task
    folder = _directory(env.workspace, task.output_prefix.rstrip('/'))
    path = folder / 'output.mp4'
    contract = planned['contract']
    expected = contract['time_map']['clock']['duration_seconds'] / contract['time_map']['speed']
    # Explicit test producer, not the future product renderer. Output is independently generated.
    env.variants.tools.ffmpeg_run(['-v', 'error', '-f', 'lavfi', '-i', f'testsrc2=size=80x48:rate=10:duration={duration or expected}',
        *(['-map', '0:v', '-map', '0:v'] if extra_video else []), '-c:v', 'libx264', '-y', str(path)], timeout=20)
    artifact = ArtifactRef(kind='video', ref=path.relative_to(env.workspace).as_posix(), sha256=sha256(path.read_bytes()).hexdigest())
    result = WorkerResult(task_id=task.task_id, attempt=task.attempt, lease_epoch=task.lease_epoch, input_fingerprint=task.input_fingerprint,
        versions=task.versions, status='succeeded', artifacts=(artifact,), elapsed_seconds=0.1)
    assert env.router.complete(result, principal=env.workers[0])['accepted']
    env.router.release(task.task_id, task.lease_epoch, principal=env.workers[0])
    return SimpleNamespace(artifact=artifact, lease=lease, path=path, job=job)


def register(env, planned, output):
    return env.variants.register_output(planned['plan_id'], output.artifact, task_id=output.lease.task.task_id,
                                       lease_epoch=output.lease.task.lease_epoch, principal=env.admin)
