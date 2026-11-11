"""One personnel action -> durable Router pipeline, current status and formal export."""
from hashlib import sha256
import json
from typing import Annotated
from pydantic import Field
from media_tag.catalog import AccessDenied,CatalogError,RevisionConflict
from media_tag.catalog.service import encode
from media_tag.contracts.models import ContractModel,Versions,TaskLimits
from media_tag.export.models import AssetSelection,ExportSubmission
from media_tag.export.service import ExportService
from media_tag.files.service import FileError
from media_tag.router.models import StepSpec,JobSpec
from media_tag.storage import StorageError
from .service import ensure,SemanticSubmission
from .contracts import PREPROCESS


def _topic_service(search):
    from media_tag.analysis.service import AnalysisService
    return getattr(search,'analysis_service',None) or AnalysisService(search.router)


def _topic_mode(search):
    service=_topic_service(search)
    return (getattr(search,'semantic_registry',None) is None or service.options.enabled
        or any(stage in search.router.stages for stage in ('local_topics','audio_topics','precision')))


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
    if _topic_mode(search):
        return _submit_topics(search,spec,principal=principal)
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
    if _topic_mode(search):
        return _topic_progress(search,spec,principal=principal)
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
    registry=getattr(search,'semantic_registry',None)
    with search.database.transaction() as c:
        suggestions=[(r['asset_id'],[tuple(v) for v in c.execute('SELECT suggestion_id,payload FROM tag_suggestions WHERE asset_id=? ORDER BY suggestion_id',(r['asset_id'],))]) for r in rows]
    request='auto-tag-export-'+sha256(encode([rows,registry.revision if registry else None,suggestions]).encode()).hexdigest()
    result=ExportService(search.reviews).create(ExportSubmission(request_id=request,assets=selection,include_ai_drafts=True),principal=principal)
    return result|{'assets_url':'/review/exports/'+result['export_id']+'/content/assets.csv'}


def _submit_topics(search,spec,*,principal):
    """Only the explicitly selected batch; never scan other actor intents here."""
    from media_tag.analysis.intake import ensure_intent
    from media_tag.analysis.repair import RepairSubmission,repair
    service=_topic_service(search)
    rows=selected(search,spec,principal);added=reused=0;errors=[];items=[]
    for row in rows:
        try:
            with search.database.transaction(write=True) as c:
                asset=search.catalog._authorize(c,row['asset_id'],principal,'edit')
                if asset['blob_sha256']!=row['source_sha256'] or search.files._gate(c,asset):
                    raise RevisionConflict('batch source changed')
                intent_id=ensure_intent(c,row['asset_id'],principal.actor_id)
                intent=c.execute('SELECT * FROM analysis_intents WHERE intent_id=?',(intent_id,)).fetchone()
                binding=c.execute('SELECT job_id FROM router_requests WHERE owner_id=? AND request_id=?',
                    (principal.actor_id,intent_id)).fetchone()
                fingerprint=c.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()[0]
            if not service.options.enabled:
                reused+=1
                items.append({'asset_id':row['asset_id'],'state':'awaiting_analysis_configuration'})
                continue
            if binding is None:
                job=search.router.submit(JobSpec(asset_id=row['asset_id'],steps=service._steps(fingerprint,intent['content_kind'])),
                    request_id=intent_id,principal=principal)
                added+=1
                items.append({'asset_id':row['asset_id'],'state':'queued','job_id':job['job_id']})
                continue
            job=search.router.get_job(binding['job_id'],principal=principal)
            if job['paused'] or job['state']=='cancelled':
                reused+=1
                items.append({'asset_id':row['asset_id'],'state':job['state']})
                continue
            if intent['content_kind'] is None:
                try:
                    adopted=service._adopt_probe(intent,binding['job_id'],fingerprint,principal)
                except (ValueError,StorageError,OSError):
                    # Failed/corrupt type discovery can itself be explicitly repaired;
                    # never manufacture a type in order to add downstream work.
                    adopted={'state':'probe_unavailable'}
                if adopted['state'] not in ('probe_adopted','probe_unavailable','failed','needs_attention'):
                    reused+=1;items.append({'asset_id':row['asset_id'],'state':adopted['state']});continue
            # A batch button is not authorization to replace old model/policy generations.
            # Dedicated repair remains available for an explicit incompatible-version retry.
            with search.database.transaction() as c:
                asset=search.catalog._authorize(c,row['asset_id'],principal,'edit')
                intent=c.execute('SELECT * FROM analysis_intents WHERE intent_id=?',(intent_id,)).fetchone()
                job=search.router._job_auth(c,binding['job_id'],principal)
                tasks=c.execute('SELECT step_key,spec_json,task_id,state,lease_epoch FROM router_tasks WHERE job_id=? AND active=1 ORDER BY step_key',
                    (job['job_id'],)).fetchall()
                desired={step.key:step for step in service._steps(fingerprint,intent['content_kind'])}
                incompatible=any(t['step_key'] in desired and StepSpec.model_validate_json(t['spec_json'])!=desired[t['step_key']] for t in tasks)
                revision=asset['revision'];job_revision=job['revision']
                basis=[tuple(t) for t in tasks]
            if incompatible:
                reused+=1;items.append({'asset_id':row['asset_id'],'state':'awaiting_explicit_version_repair'});continue
            request='auto-tag-repair-'+sha256(encode([row['asset_id'],row['source_sha256'],revision,job_revision,basis]).encode()).hexdigest()
            reply=repair(service,row['asset_id'],RepairSubmission(request_id=request,expected_asset_revision=revision,
                source_sha256=row['source_sha256'],expected_job_revision=job_revision),principal=principal)
            if reply['replanned_keys']:added+=1
            else:reused+=1
            waiting=next((value for value in reply['stage_assessments'].values() if value.startswith('awaiting_current_')),None)
            items.append({'asset_id':row['asset_id'],'state':waiting or ('queued' if reply['replanned_keys'] else 'reused')})
        except (CatalogError,StorageError,OSError,ValueError,FileError):
            errors.append({'asset_id':row['asset_id'],'code':'analysis_not_ready_or_changed'})
    return {'state':'partial' if errors else 'queued' if added else 'pending','total':len(rows),'added':added,
        'reused':reused,'errors':errors,'items':items,'medical_confirmation':False,'effective_values_written':False}


def _topic_progress(search,spec,*,principal):
    service=_topic_service(search)
    rows=selected(search,spec,principal)
    counts=dict(total=len(rows),completed=0,running=0,failed=0,not_requested=0,waiting=0,
        pending_capability=0,pending_fields=0,review_required=0)
    items=[];failed=[];semantic_complete=True
    for row in rows:
        state=service.status(row['asset_id'],principal=principal)
        runtime=state.get('runtime');document=state.get('topic_analysis') or {}
        fields=document.get('fields',[])
        unresolved=[f for f in fields if f.get('next_step')!='none']
        capability=[f for f in unresolved if f.get('next_step')=='provider' or 'not_configured' in f.get('reason','')]
        review=[f for f in fields if f.get('requires_field_review') or f.get('next_step') in ('human','review','taxonomy')]
        pending_fields=len(unresolved) if fields else 10
        pending_capability=bool(capability or not service.options.enabled)
        if any(f.get('next_step')=='precision' for f in unresolved):
            from media_tag.models.contracts import ModelFailure
            from media_tag.precision.runtime import provider_for
            try:
                provider_for(service.precision_deployment,state.get('content_kind'))
            except (ModelFailure,AttributeError):
                pending_capability=True
        counts['pending_fields']+=pending_fields
        counts['pending_capability']+=pending_capability
        counts['review_required']+=bool(review)
        complete=bool(state.get('analysis_complete'))
        semantic_complete &= complete
        if runtime:
            with search.database.transaction() as c:
                tasks=c.execute('''SELECT t.state,t.spec_json,a.released_at,a.result_json FROM router_tasks t
                    LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                    WHERE t.job_id=? AND t.active=1''',(runtime['job_id'],)).fetchall()
                held=c.execute('SELECT 1 FROM router_holds h JOIN router_tasks t USING(task_id) WHERE t.job_id=? LIMIT 1',(runtime['job_id'],)).fetchone()
            stopped=runtime['state'] in ('paused','cancelled')
            live=bool(held) or not stopped and any(t['state'] in ('queued','leased','running','retry_wait')
                or t['result_json'] and t['released_at'] is None for t in tasks)
            broken=any(t['state'] in ('failed','needs_attention','cancelled') and not _analysis_complete(t) for t in tasks)
            key='running' if live else 'failed' if broken or stopped else 'completed'
        else:
            key='not_requested' if state['state']=='not_requested' else 'completed'
        counts[key]+=1
        if key=='failed':failed.append(row['asset_id'])
        if not complete and key!='running':counts['waiting']+=1
        items.append({'asset_id':row['asset_id'],'state':state['state'],'execution_state':key,
            'semantic_complete':complete,'pending_fields':pending_fields,'pending_capability':pending_capability,
            'review_required':bool(review)})
    terminal=counts['running']==0
    return {'state':'ready' if terminal and not counts['failed'] else 'partial' if counts['failed'] else 'running',
        **counts,'failed_asset_ids':failed,'items':items,'execution_complete':terminal,
        'semantic_complete':bool(semantic_complete),'medical_confirmation':False,
        'human_review_required_for_completion':False,'completion_scope':'execution_only'}
