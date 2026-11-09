"""Explicitly add a missing/failed semantic stage, preserving all successful predecessors."""
from pydantic import Field
from typing import Annotated
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.contracts.models import ContractModel,Identifier,Sha256,Versions,TaskLimits
from media_tag.files.service import FileError
from media_tag.router.models import StepSpec
from .contracts import FIELDS,PREPROCESS
from .prompt import PROMPT_VERSION


class SemanticSubmission(ContractModel):
    job_id: Identifier
    expected_revision: Annotated[int,Field(ge=1)]
    source_sha256: Sha256


def ensure(search,asset_id,spec,*,principal):
    if principal.role not in ('editor','admin'):raise AccessDenied('editor required')
    registry=getattr(search,'semantic_registry',None)
    if registry is None:raise FileError(503,'semantic_worker_unavailable')
    router=search.router
    with router.database.transaction() as c:
        asset=router.catalog._authorize(c,asset_id,principal,'edit')
        if asset['blob_sha256']!=spec.source_sha256 or asset['revision']!=spec.expected_revision:raise RevisionConflict('asset changed')
        if search.files._gate(c,asset):raise FileError(409,'source_unavailable')
        job=router._job_auth(c,spec.job_id,principal)
        if job['asset_id']!=asset_id or job['cancelled']:raise RevisionConflict('analysis job changed')
        rows=c.execute('SELECT * FROM router_tasks WHERE job_id=? AND active=1',(spec.job_id,)).fetchall()
        evidence=next((r for r in rows if r['step_key']=='evidence'),None)
        if evidence is None:raise RevisionConflict('current evidence required')
        existing=next((r for r in rows if r['step_key']=='visual_fields'),None)
        if existing:
            step=StepSpec.model_validate_json(existing['spec_json'])
            if step.versions.model_revision==registry.revision and step.versions.preprocess_version==PREPROCESS and existing['state'] in ('queued','leased','running','retry_wait','succeeded'):
                return {'job_id':spec.job_id,'task_id':existing['task_id'],'state':existing['state'],'reused':True}
        revision=job['revision']
    step=StepSpec(key='visual_fields',stage='visual_fields',depends_on=('evidence',),target_fields=FIELDS,
        versions=Versions(rules_version=router.catalog.contract.version,preprocess_version=PREPROCESS,model_revision=registry.revision),
        limits=TaskLimits(gpu_slots=1),prompt_version=PROMPT_VERSION)
    view=router.replan(spec.job_id,(step,),expected_revision=revision,principal=principal)
    task=next(t for t in view['tasks'] if t['key']=='visual_fields')
    return {'job_id':spec.job_id,'task_id':task['task_id'],'state':task['state'],'reused':False}
