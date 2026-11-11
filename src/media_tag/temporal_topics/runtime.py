"""Opt-in Router worker for complete bounded video/animation topic evidence."""
from hashlib import sha256
import json
import os
from pathlib import Path
import time

from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.contracts.models import ArtifactRef,TagSuggestion
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StageDefinition,StepSpec
from media_tag.semantic.registry import SemanticRegistry
from media_tag.workers.evidence import _directory
from .contracts import TaskOptions,Prepared,Generation,RouterReport
from .native import _processor_identity
from .process import run_child
from .prompt import PROMPT_VERSION,PREPROCESS_VERSION,prompts
from .service import TemporalTopicService
from .clocks import common_clock,check_window

VERSION='ct-temporal-worker-v1'


def tool_digest(tools):
    return sha256(json.dumps({'tools':tools.identity,'processor':_processor_identity(),
        'preprocess':PREPROCESS_VERSION},sort_keys=True).encode()).hexdigest()


def stage(registry,tools):
    return StageDefinition(TaskOptions,VERSION+':'+tool_digest(tools),metadata_sensitive=False,
        required_preprocess_version=PREPROCESS_VERSION,required_model_revision=registry.revision,
        allowed_target_fields=TOPIC_FIELDS,required_gpu_slots=1,gpu_allowed=True)


def suggestions(document,task):
    return tuple(TagSuggestion(suggestion_id='temporal-topic-'+sha256(
        f'{task.task_id}:{task.lease_epoch}:{item.field}'.encode()).hexdigest()[:32],field=item.field,
        value=item.value,origin='ai',producer_version=PROMPT_VERSION,evidence_ids=item.evidence_ids)
        for item in document.fields if item.field in task.target_fields)


def read_artifact(router,ref,maximum,*,retain=True):
    with router.files.open_read(ref.ref) as stream:
        before=os.fstat(stream.fileno()).st_size
        if before>maximum:raise ValueError('temporal_artifact_budget')
        digest,count,chunks=sha256(),0,[]
        while chunk:=stream.read(min(1024**2,maximum-count+1)):
            count+=len(chunk)
            if count>maximum:raise ValueError('temporal_artifact_budget')
            digest.update(chunk)
            if retain:chunks.append(chunk)
        if digest.hexdigest()!=ref.sha256 or os.fstat(stream.fileno()).st_size!=before:
            raise ValueError('temporal_artifact_integrity')
    return b''.join(chunks) if retain else None


def limits(options):
    return {'temporal_router_report':2*1024**2,'temporal_raw_report':1024**2,
        'temporal_input_manifest':1024**2,'temporal_source':options.max_input_bytes,
        'temporal_model_frame':options.model_edge**2*4+65536}


def check_artifacts(router,artifacts,prefix,options):
    caps=limits(options)
    if len(artifacts)>options.max_source_frames+4:raise ValueError('temporal_artifact_count_budget')
    for artifact in artifacts:
        if artifact.kind not in caps or not artifact.ref.startswith(prefix):raise ValueError('temporal_attempt_artifact_required')
        read_artifact(router,artifact,caps[artifact.kind],retain=False)


def child_request(source,output,root,kind,options,tools,*,seconds,mode):
    return {'mode':mode,'source_path':str(source),'output':str(output),'artifact_root':str(root),
        'source_sha256':None,'content_kind':kind,'options':options.model_dump(mode='json'),
        'tool_dir':str(tools.ffmpeg.parent),'tool_identity':sha256(json.dumps(tools.identity,sort_keys=True).encode()).hexdigest(),
        'timeout_seconds':seconds,'prompt_version':PROMPT_VERSION}


class TemporalTopicHandlers:
    def __init__(self,router,worker,deployment,tools):
        self.router,self.worker,self.deployment,self.tools=router,worker,deployment,tools
        self.registry=SemanticRegistry(Path(deployment.model_root)) if deployment.model_root else None
        # Trusted bootstrap/handler state, never a caller-selected executable path.
        router.temporal_topic_tools=tools

    def __call__(self,lease,stopped):
        task=lease.task;options=TaskOptions.model_validate(lease.config)
        deadline=time.monotonic()+task.limits.timeout_seconds-min(2.,task.limits.timeout_seconds*.25)
        def check():
            if stopped.is_set():raise ModelFailure('cancelled')
            if time.monotonic()>=deadline:raise ModelFailure('temporal_timeout')
        check();policy=getattr(self.router.catalog,'review_policy',None)
        with self.router.database.transaction() as connection:
            row=connection.execute('SELECT spec_json FROM router_tasks WHERE task_id=? AND active=1 AND lease_epoch=?',
                (task.task_id,task.lease_epoch)).fetchone()
            source_size=connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?',(task.input_refs[0].sha256,)).fetchone()
        spec=StepSpec.model_validate_json(row['spec_json']) if row else None
        definition=self.router.stages.get('temporal_topics')
        if (not self.deployment.enabled or self.registry is None or policy is None or options.policy_digest!=policy.digest
                or self.registry.revision!=options.model_revision or task.versions.model_revision!=self.registry.revision
                or task.versions.rules_version!='sha256:'+policy.digest or task.versions.taxonomy_version!=policy.config.taxonomy_version
                or task.versions.preprocess_version!=PREPROCESS_VERSION or task.limits.gpu_slots!=1
                or spec is None or spec.prompt_version!=PROMPT_VERSION or spec.target_fields!=task.target_fields
                or definition is None or definition.implementation_version!=VERSION+':'+options.tool_digest
                or definition.required_model_revision!=options.model_revision
                or definition.required_preprocess_version!=PREPROCESS_VERSION
                or not task.target_fields or not set(task.target_fields)<=set(TOPIC_FIELDS)
                or options.tool_digest!=tool_digest(self.tools)):
            raise ModelFailure('temporal_configuration_changed')
        if source_size is None or source_size[0]>options.options.max_input_bytes:raise ModelFailure('temporal_input_budget')
        probes=[ref for ref in task.input_refs if ref.kind=='technical_report']
        if len(probes)!=1:raise ModelFailure('temporal_verified_probe_required')
        probe=json.loads(read_artifact(self.router,probes[0],1024**2))
        if {'gif':'animation'}.get(probe.get('kind'),probe.get('kind'))!=options.content_kind:
            raise ModelFailure('temporal_modality_changed')
        clock_origin,clock_span=common_clock(probe,options.content_kind)
        output=_directory(self.router.workspace,task.output_prefix.rstrip('/'))
        source_dir=_directory(output,'source');drafts=_directory(output,'drafts')
        (output/'README.md').write_text('Private continuous-topic child attempt; no approvals.\n')
        (source_dir/'README.md').write_text('Private read-only source copy for bounded child decoding.\n')
        source_path=source_dir/'input.bin';digest,count=sha256(),0
        with self.router.open_input(task.task_id,task.lease_epoch,principal=self.worker) as source,source_path.open('xb') as target:
            while chunk:=source.read(min(1024**2,options.options.max_input_bytes-count+1)):
                check();count+=len(chunk)
                if count>options.options.max_input_bytes:raise ModelFailure('temporal_input_budget')
                digest.update(chunk);target.write(chunk)
        if digest.hexdigest()!=task.input_refs[0].sha256:raise ModelFailure('temporal_source_integrity')
        request=child_request(source_path,drafts,self.router.workspace,options.content_kind,options.options,self.tools,
            seconds=max(.01,deadline-time.monotonic()),mode='infer')
        request.update(source_sha256=digest.hexdigest(),model_root=str(self.registry.root),model_revision=self.registry.revision,
            field_prompts=prompts(policy,options.options),source_clock_origin=clock_origin.model_dump(mode='json'),
            source_clock_span=clock_span)
        response=self.invoke(request,output,task,stopped,deadline=deadline)
        if set(response)!={'ok','prepared','generation'}:raise ModelFailure('temporal_response_binding')
        prepared=Prepared.model_validate_json(json.dumps(response['prepared']));generation=Generation.model_validate_json(json.dumps(response['generation']))
        if prepared.source.sha256!=digest.hexdigest() or prepared.sequence.source_sha256!=digest.hexdigest():
            raise ModelFailure('temporal_child_source_changed')
        check_window(prepared.sequence,clock_origin,clock_span)
        if generation.backend!='local_qwen_video':raise ModelFailure('temporal_synthetic_result_rejected')
        if generation.processor_identity!=_processor_identity():raise ModelFailure('temporal_processor_changed')
        service=TemporalTopicService(drafts,policy=policy,generator=None,options=options.options,artifact_root=self.router.workspace)
        local=service.finish(prepared,generation,asset_id=task.asset_id,asset_revision=task.asset_revision,
            analysis_id='temporal-topic-'+sha256(f'{task.task_id}:{task.lease_epoch}'.encode()).hexdigest(),
            content_kind=options.content_kind,expected_model_revision=self.registry.revision,versions=task.versions,
            source_clock_origin=clock_origin)
        report=RouterReport(task_id=task.task_id,lease_epoch=task.lease_epoch,input_fingerprint=task.input_fingerprint,
            versions=task.versions,tool_digest=options.tool_digest,prepared=prepared,result=local)
        raw=report.model_dump_json().encode()
        if len(raw)>2*1024**2:raise ModelFailure('temporal_router_report_budget')
        path=output/'temporal-topics.json';path.write_bytes(raw)
        ref=ArtifactRef(kind='temporal_router_report',ref=path.relative_to(self.router.workspace).as_posix(),sha256=sha256(raw).hexdigest())
        artifacts=tuple(dict.fromkeys((ref,prepared.source,local.manifest,local.raw_report,
            *(frame.model_image for frame in local.sequence.frames))))
        check_artifacts(self.router,artifacts,task.output_prefix,options.options);check()
        return {'status':'succeeded','artifacts':artifacts,'evidence':local.document.evidence,
            'suggestions':suggestions(local.document,task)}

    def invoke(self,request,output,task,stopped,*,deadline):
        return run_child(request,output,stopped,deadline=deadline)
