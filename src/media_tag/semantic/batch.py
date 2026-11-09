"""One personnel action -> durable Router pipeline, current status and formal export."""
from hashlib import sha256
import json
from typing import Annotated
from pydantic import Field
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.catalog.service import encode
from media_tag.contracts.models import ContractModel,Versions,TaskLimits
from media_tag.export.models import AssetSelection,ExportSubmission
from media_tag.export.service import ExportService
from media_tag.files.service import FileError
from media_tag.router.models import StepSpec,JobSpec
from .service import ensure,SemanticSubmission
from .contracts import PREPROCESS


class AutoTagScope(ContractModel):
    # Empty = explicitly all my current active library assets, never other owners.
    assets: Annotated[tuple[AssetSelection,...],Field(max_length=500)]=()


def selected(search,spec,principal):
    if principal.role not in ('editor','admin'):raise AccessDenied('editor required')
    with search.database.transaction() as c:
        if spec.assets:
            if len({a.asset_id for a in spec.assets})!=len(spec.assets):raise ValueError('duplicate selection')
            rows=[]
            for selection in spec.assets:
                row=search.catalog._authorize(c,selection.asset_id,principal,'edit')
                if row['blob_sha256']!=selection.source_sha256:raise RevisionConflict('source changed')
                rows.append(row)
        else:
            rows=c.execute('''SELECT a.* FROM assets a LEFT JOIN library_entries e USING(asset_id)
                WHERE a.owner_id=? AND a.deleted_at IS NULL AND coalesce(e.active,EXISTS(
                    SELECT 1 FROM uploads u WHERE u.asset_id=a.asset_id AND u.state='completed'
                    AND json_extract(u.spec_json,'$.purpose')='library'))=1 ORDER BY a.asset_id LIMIT 501''',(principal.actor_id,)).fetchall()
        if not rows or len(rows)>500:raise ValueError('select 1 to 500 owned assets')
        result=[]
        for row in rows:
            if row['owner_id']!=principal.actor_id:raise AccessDenied('batch auto-tag is owner scoped')
            if search.files._gate(c,row):raise FileError(409,'source_unavailable')
            job=c.execute('''SELECT job_id FROM router_jobs WHERE asset_id=? AND owner_id=? AND cancelled=0
                ORDER BY created_at DESC,rowid DESC LIMIT 1''',(row['asset_id'],principal.actor_id)).fetchone()
            result.append({'asset_id':row['asset_id'],'source_sha256':row['blob_sha256'],'expected_revision':row['revision'],
                'job_id':job[0] if job else None})
        return result


def submit(search,spec,*,principal):
    registry=getattr(search,'semantic_registry',None)
    if registry is None:raise FileError(503,'semantic_worker_unavailable')
    rows=selected(search,spec,principal);added=reused=0;errors=[]
    for row in rows:
        try:
            if row['job_id'] is None:
                versions=Versions(rules_version=search.catalog.contract.version,preprocess_version='d02-cpu-v1')
                steps=[StepSpec(key='probe',stage='probe',config={'max_pixels':search.library.probe_max_pixels},versions=versions),
                    StepSpec(key='evidence',stage='evidence',depends_on=('probe',),versions=versions)]
                for kind in ('ocr','face','clip','visual_fields'):
                    definition=search.router.stages.get(kind)
                    if definition and definition.required_model_revision:
                        steps.append(StepSpec(key=kind,stage=kind,depends_on=('evidence',),target_fields=definition.allowed_target_fields or (),
                            versions=Versions(rules_version=versions.rules_version,preprocess_version=definition.required_preprocess_version,model_revision=definition.required_model_revision),
                            limits=TaskLimits(gpu_slots=definition.required_gpu_slots)))
                request='auto-tag-'+sha256(encode([row['asset_id'],row['source_sha256'],registry.revision]).encode()).hexdigest()
                search.router.submit(JobSpec(asset_id=row['asset_id'],steps=tuple(steps)),request_id=request,principal=principal);added+=1
            else:
                reply=ensure(search,row['asset_id'],SemanticSubmission.model_validate({k:v for k,v in row.items() if k!='asset_id'}),principal=principal)
                if reply['reused']:reused+=1
                else:added+=1
        except (ValueError,RevisionConflict,FileError):errors.append({'asset_id':row['asset_id'],'code':'analysis_not_ready_or_changed'})
    return {'state':'queued' if not errors else 'partial','total':len(rows),'added':added,'reused':reused,'errors':errors}


def progress(search,spec,*,principal):
    rows=selected(search,spec,principal);counts={'total':len(rows),'completed':0,'running':0,'failed':0,'not_requested':0};errors=[]
    with search.database.transaction() as c:
        for row in rows:
            tasks=c.execute('''SELECT t.state,t.spec_json,a.released_at,a.result_json FROM router_tasks t JOIN router_jobs j USING(job_id)
                LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                WHERE j.job_id=? AND j.cancelled=0 AND t.active=1''',(row['job_id'],)).fetchall()
            task=next((t for t in tasks if json.loads(t['spec_json'])['stage']=='visual_fields'),None)
            current=task is not None and json.loads(task['spec_json'])['versions']['preprocess_version']==PREPROCESS
            usable=[_analysis_complete(t) for t in tasks]
            failed=any(t['state'] in ('failed','needs_attention','cancelled') and not done for t,done in zip(tasks,usable))
            complete=tasks and all(usable)
            key='not_requested' if not current else 'failed' if failed else 'completed' if complete else 'running'
            counts[key]+=1
            if key=='failed':errors.append(row['asset_id'])
    return {'state':'ready' if counts['completed']==counts['total'] else 'partial' if counts['failed'] else 'running',**counts,'failed_asset_ids':errors,
        'medical_confirmation':False,'human_review_required_for_completion':False}


def _analysis_complete(task):
    if task['released_at'] is None:return False
    if task['state']=='succeeded':return True
    # CLIP vectors remain usable while the separately released classification vocabulary is absent.
    if task['state']=='needs_attention' and json.loads(task['spec_json'])['stage']=='clip' and task['result_json']:
        result=json.loads(task['result_json']);errors=result.get('errors',[])
        return result.get('status')=='partial' and bool(errors) and all(e.get('code')=='unpublished_vocabulary' for e in errors)
    return False


def export(search,spec,*,principal):
    state=progress(search,spec,principal=principal)
    if state['state']!='ready':raise RevisionConflict('auto-tag still running or needs attention')
    rows=selected(search,spec,principal)
    selection=tuple(AssetSelection(**{k:v for k,v in r.items() if k!='job_id'}) for r in rows)
    registry=search.semantic_registry
    with search.database.transaction() as c:
        suggestions=[(r['asset_id'],[tuple(v) for v in c.execute('SELECT suggestion_id,payload FROM tag_suggestions WHERE asset_id=? ORDER BY suggestion_id',(r['asset_id'],))]) for r in rows]
    request='auto-tag-export-'+sha256(encode([rows,registry.revision,suggestions]).encode()).hexdigest()
    result=ExportService(search.reviews).create(ExportSubmission(request_id=request,assets=selection,include_ai_drafts=True),principal=principal)
    return result|{'assets_url':'/review/exports/'+result['export_id']+'/content/assets.csv'}
