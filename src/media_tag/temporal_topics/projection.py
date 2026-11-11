"""Strict production report verification and rebuildable analysis projection."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

from media_tag.analysis.contracts import AnalysisProducer,FieldAssessment,TOPIC_FIELDS
from media_tag.contracts.models import FieldValue,TaskEnvelope,WorkerResult,validate_result_for_task
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StepSpec
from media_tag.semantic.registry import SemanticRegistry
from media_tag.storage import StorageError
from .contracts import Generation,Prepared,RouterReport,TaskOptions
from .native import _processor_identity
from .normalizer import assess
from .process import run_child
from .prompt import PREPROCESS_VERSION,PROMPT_VERSION,prompts
from .runtime import VERSION,child_request,limits,read_artifact,suggestions,tool_digest
from .clocks import common_clock,check_window


def current_row(router,task_id):
    with router.database.transaction() as connection:
        row=connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at,j.cancelled,j.paused
            FROM router_tasks t JOIN router_jobs j USING(job_id)
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.task_id=?''',(task_id,)).fetchone()
    return dict(row) if row else None


def sequence_content(sequence):
    """Compare actual content/clock identity independently of private storage paths."""
    payload=sequence.model_dump(mode='json')
    for frame in payload['frames']:frame['model_image'].pop('ref')
    return payload


def redecoded(router,source,options,tools):
    # Both source re-decoding and its native tools run in a bounded child; no GPU.
    with TemporaryDirectory(prefix='temporal-verify-',dir=router.workspace) as location:
        folder=Path(location);(folder/'README.md').write_text('Private disposable bounded continuous-source revalidation.\n')
        source_dir=folder/'source';source_dir.mkdir();(source_dir/'README.md').write_text('Private verified input copy.\n')
        path=source_dir/'input.bin';path.write_bytes(source)
        output=folder/'drafts';output.mkdir()
        request=child_request(path,output,router.workspace,options.content_kind,options.options,tools,
            seconds=min(60.,options.options.tool_timeout_seconds+10.),mode='decode')
        request['source_sha256']=sha256(source).hexdigest()
        response=run_child(request,folder,threading.Event(),deadline=time.monotonic()+min(60.,options.options.tool_timeout_seconds+10.))
        if set(response)!={'ok','prepared'}:raise ValueError('temporal_redecode_response_changed')
        return Prepared.model_validate_json(json.dumps(response['prepared'])).sequence


def verified(router,row,asset,*,tools=None):
    task=TaskEnvelope.model_validate_json(row['envelope_json']);result=WorkerResult.model_validate_json(row['result_json'])
    spec=StepSpec.model_validate_json(row['spec_json']);options=TaskOptions.model_validate(spec.config)
    current=current_row(router,task.task_id)
    if (current is None or not current['active'] or current['cancelled'] or current['paused']
            or current['state']!='succeeded' or current['released_at'] is None
            or current['lease_epoch']!=task.lease_epoch or current['envelope_json']!=row['envelope_json']
            or current['result_json']!=row['result_json'] or current['spec_json']!=row['spec_json']
            or task.task_id!=row['task_id'] or task.job_id!=row['job_id'] or current['job_id']!=task.job_id
            or task.stage!='temporal_topics' or spec.stage!=task.stage or task.asset_id!=asset['asset_id']
            or task.input_refs[0].sha256!=asset['blob_sha256'] or spec.versions!=task.versions
            or spec.target_fields!=task.target_fields or not task.target_fields or not set(task.target_fields)<=set(TOPIC_FIELDS)
            or result.status!='succeeded'):
        raise ValueError('temporal_current_attempt_changed')
    validate_result_for_task(task,result)
    tools=tools or getattr(router,'temporal_topic_tools',None)
    policy=getattr(router.catalog,'review_policy',None);definition=router.stages.get('temporal_topics')
    expected_revision=SemanticRegistry(Path('.')).revision
    if (tools is None or policy is None or definition is None
            or options.tool_digest!=tool_digest(tools) or definition.implementation_version!=VERSION+':'+options.tool_digest
            or definition.required_model_revision!=expected_revision or definition.required_preprocess_version!=PREPROCESS_VERSION
            or options.policy_digest!=policy.digest or options.model_revision!=expected_revision
            or task.versions.model_revision!=expected_revision or task.versions.preprocess_version!=PREPROCESS_VERSION
            or task.versions.rules_version!='sha256:'+policy.digest or task.versions.taxonomy_version!=policy.config.taxonomy_version
            or task.limits.gpu_slots!=1 or spec.prompt_version!=PROMPT_VERSION):
        raise ValueError('temporal_current_configuration_changed')
    probes=[ref for ref in task.input_refs if ref.kind=='technical_report']
    if len(probes)!=1:raise ValueError('temporal_verified_probe_required')
    probe=json.loads(read_artifact(router,probes[0],1024**2))
    if {'gif':'animation'}.get(probe.get('kind'),probe.get('kind'))!=options.content_kind:
        raise ValueError('temporal_modality_changed')
    clock_origin,clock_span=common_clock(probe,options.content_kind)
    if len(result.artifacts)>options.options.max_source_frames+4:raise ValueError('temporal_artifact_count_budget')
    refs=[ref for ref in result.artifacts if ref.kind=='temporal_router_report']
    if len(refs)!=1 or not refs[0].ref.startswith(task.output_prefix):raise ValueError('one_temporal_attempt_report_required')
    report=RouterReport.model_validate_json(read_artifact(router,refs[0],2*1024**2))
    local=report.result;document=local.document;sequence=local.sequence;prepared=report.prepared
    expected=tuple(dict.fromkeys((refs[0],prepared.source,local.manifest,local.raw_report,
        *(frame.model_image for frame in sequence.frames))))
    if len(result.artifacts)!=len(expected) or set(result.artifacts)!=set(expected):raise ValueError('exact_temporal_artifacts_required')
    caps=limits(options.options)
    for artifact in result.artifacts:
        if artifact.kind not in caps or not artifact.ref.startswith(task.output_prefix):raise ValueError('temporal_attempt_artifact_required')
        if artifact!=refs[0]:read_artifact(router,artifact,caps[artifact.kind],retain=False)
    if (report.task_id!=task.task_id or report.lease_epoch!=task.lease_epoch
            or report.input_fingerprint!=task.input_fingerprint or report.versions!=task.versions
            or report.tool_digest!=options.tool_digest or prepared.sequence!=sequence or prepared.manifest!=local.manifest
            or prepared.source.kind!='temporal_source' or prepared.source.sha256!=asset['blob_sha256']
            or sequence.source_sha256!=asset['blob_sha256'] or local.backend!='local_qwen_video'
            or document.asset_id!=asset['asset_id'] or document.asset_revision!=task.asset_revision
            or document.content_kind!=options.content_kind or document.content_sha256!=asset['blob_sha256']
            or local.source_clock_origin!=clock_origin
            or result.evidence!=document.evidence or result.suggestions!=suggestions(document,task)
            or len(sequence.frames)>options.options.max_source_frames
            or len(sequence.model_frame_sources)>options.options.max_model_frames
            or sequence.duration.fraction()>options.options.max_seconds):
        raise ValueError('temporal_report_binding_changed')
    check_window(sequence,clock_origin,clock_span)
    manifest=json.loads(read_artifact(router,local.manifest,1024**2))
    if manifest!={'sequence':sequence.model_dump(mode='json'),'source':prepared.source.model_dump(mode='json')}:
        raise ValueError('temporal_manifest_changed')
    raw=json.loads(read_artifact(router,local.raw_report,1024**2));generation=Generation.model_validate_json(json.dumps(raw['generation']))
    if (raw['schema_version']!='ct-temporal-raw-v1' or raw['source']!=prepared.source.model_dump(mode='json')
            or raw['manifest']!=local.manifest.model_dump(mode='json') or raw['versions']!=task.versions.model_dump(mode='json')
            or raw['prompt_version']!=PROMPT_VERSION or raw['policy_digest']!=policy.digest
            or raw['questions']!=list(prompts(policy,options.options)) or raw['options']!=options.options.model_dump(mode='json')
            or raw['audio_policy']!='not_analyzed' or any(raw[name] is not False for name in
                ('medical_confirmation','privacy_confirmation','effective_values_written'))
            or generation.backend!='local_qwen_video' or generation.model_revision!=expected_revision
            or generation.prompt_version!=PROMPT_VERSION or generation.processor_identity!=_processor_identity()
            or generation.input_sha256!=sequence.input_sha256):
        raise ValueError('temporal_raw_report_binding_changed')
    if raw['source_clock_origin']!=clock_origin.model_dump(mode='json'):raise ValueError('temporal_source_clock_changed')
    fields=tuple(assess(answer,field,policy=policy,versions=task.versions,evidence_id=document.evidence[0].evidence_id)
        for field,answer in zip(TOPIC_FIELDS,generation.answers,strict=True))
    if fields!=document.fields:raise ValueError('temporal_normalization_changed')
    source=read_artifact(router,prepared.source,options.options.max_input_bytes)
    actual=redecoded(router,source,options,tools)
    if sequence_content(actual)!=sequence_content(sequence):raise ValueError('temporal_sequence_not_decoded_from_source')
    if current_row(router,task.task_id)!=current:raise ValueError('temporal_producer_changed_during_verification')
    return task,result,report,refs[0]


def merge(router,connection,asset,kind,job,fields,evidence,producers):
    # Caller must authorize the asset/file. Parent integration may supply the same
    # global scope rule after registering temporal_topics in analysis/scopes.py.
    rows=connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at FROM router_tasks t
        LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
        WHERE t.job_id=? AND t.active=1 ORDER BY t.rowid DESC''',(job['job_id'],)).fetchall()
    winners={}
    for row in rows:
        spec=StepSpec.model_validate_json(row['spec_json'])
        if spec.stage in ('visual_fields','local_topics','precision','temporal_topics'):
            for name in spec.target_fields:winners.setdefault(name,row['task_id'])
    evidence,producers=list(evidence),list(producers);states={}
    for row in rows:
        spec=StepSpec.model_validate_json(row['spec_json'])
        if spec.stage!='temporal_topics':continue
        selected={name for name in spec.target_fields if winners.get(name)==row['task_id']}
        if not selected:continue
        for name in selected:
            fields[name]=FieldAssessment(field=name,value=FieldValue(state='unknown',reason='temporal_task_pending'),
                next_step='local',reason='temporal_task_pending')
        states[row['step_key']]=row['state']
        if job['cancelled'] or job['paused']:
            state='cancelled' if job['cancelled'] else 'paused';states[row['step_key']]=state
            for name in selected:
                fields[name]=FieldAssessment(field=name,value=FieldValue(state='failed' if job['cancelled'] else 'unknown',
                    reason='temporal_task_'+state),next_step='local',reason='temporal_task_'+state)
            continue
        if row['state']!='succeeded' or row['released_at'] is None:
            if row['state'] in ('failed','needs_attention'):
                for name in selected:
                    fields[name]=FieldAssessment(field=name,value=FieldValue(state='failed',reason='temporal_task_failed'),
                        next_step='local',reason='temporal_task_failed')
            continue
        try:
            if kind not in ('video','animation'):raise ValueError('temporal_continuous_only')
            task,result,report,ref=verified(router,row,asset)
            document=report.result.document;producer_id=f'{task.task_id}.{task.lease_epoch}'
            mapping={sample.evidence_id:'temporal-'+sha256(f'{producer_id}:{sample.evidence_id}'.encode()).hexdigest()[:32]
                for sample in document.evidence}
            adopted=tuple(sample.model_copy(update={'evidence_id':mapping[sample.evidence_id]}) for sample in document.evidence)
            for item in document.fields:
                if item.field in selected:
                    fields[item.field]=item.model_copy(update={'evidence_ids':tuple(mapping[key] for key in item.evidence_ids),
                        'producer_ids':(producer_id,)})
            evidence.extend(adopted)
            producers.append(AnalysisProducer(producer_id=producer_id,task_id=task.task_id,stage=task.stage,
                lease_epoch=task.lease_epoch,source_sha256=asset['blob_sha256'],input_fingerprint=task.input_fingerprint,
                result_sha256=sha256(row['result_json'].encode()).hexdigest(),report=ref,versions=task.versions,
                prompt_version=PROMPT_VERSION,evidence_ids=tuple(sample.evidence_id for sample in adopted)))
            states[row['step_key']]='verified_temporal_topic_draft'
        except (StorageError,OSError,ValueError,KeyError,TypeError,ModelFailure):
            for name in selected:
                fields[name]=FieldAssessment(field=name,value=FieldValue(state='failed',reason='temporal_evidence_unavailable'),
                    next_step='evidence',reason='temporal_evidence_unavailable')
            states[row['step_key']]='unavailable'
    return fields,tuple(evidence),tuple(producers),states
