"""Lease-bound local VLM. Models propose fields; Router adopts suggestions atomically."""
from collections import OrderedDict
from hashlib import sha256
import json
import os
import subprocess
import sys
import time
from media_tag.contracts.models import ArtifactRef,Evidence,TagSuggestion,FieldValue
from media_tag.models.contracts import ModelFailure,FrameInput
from media_tag.models.runtime import read_json
from media_tag.router.models import StageDefinition,StepSpec
from media_tag.workers.evidence import _directory
from .contracts import SemanticOptions,Caption,FIELDS,FORMS,PREPROCESS
from .prompt import PROMPT_VERSION


def semantic_stage(registry):
    def reduce(config):
        options=SemanticOptions.model_validate(config)
        return options.model_copy(update={'max_pixels':max(128**2,options.max_pixels//2)}).model_dump()
    return StageDefinition(SemanticOptions,'visual-fields-v1:size-v2:microbatch-v1:'+registry.revision+':'+PROMPT_VERSION,
        gpu_allowed=True,metadata_sensitive=False,required_model_revision=registry.revision,
        required_preprocess_version=PREPROCESS,required_gpu_slots=1,allowed_target_fields=FIELDS,reduce_config=reduce)


def suggestions(caption,task,evidence_id):
    values=(caption.subject,caption.form,','.join(caption.keywords_zh),','.join(caption.keywords_en))
    return tuple(TagSuggestion(suggestion_id='visual-'+sha256(f'{task.task_id}:{task.lease_epoch}:{field}'.encode()).hexdigest()[:32],
        field=field,value=FieldValue(state='observed',values=(value,)) if value else FieldValue(state='unknown',reason='model_could_not_determine_from_sample'),
        origin='ai',producer_version=PROMPT_VERSION,evidence_ids=(evidence_id,) if value else ()) for field,value in zip(FIELDS,values))


class SemanticHandlers:
    def __init__(self,router,registry,*,batch_size=2):
        self.router,self.registry=router,registry;self.batch_size=batch_size;self.cache=OrderedDict()

    def __call__(self,lease,stopped):
        task=lease.task
        if task.target_fields!=FIELDS:raise ModelFailure('semantic_target_fields_mismatch')
        if task.versions.model_revision!=self.registry.revision or task.versions.preprocess_version!=PREPROCESS:
            raise ModelFailure('semantic_version_mismatch')
        policy=getattr(self.router.catalog,'review_policy',None)
        if policy is None or policy.options(policy.asset_fields['表现形式'])!=FORMS:raise ModelFailure('semantic_template_mismatch')
        refs=[r for r in task.input_refs if r.kind=='evidence_manifest']
        if len(refs)!=1:raise ModelFailure('missing_evidence_manifest')
        manifest=read_json(self.router.files,refs[0]);frames=manifest.get('frames',[])
        if manifest.get('status')=='not_applicable':
            folder=_directory(self.router.workspace,task.output_prefix.rstrip('/'))
            (folder/'README.md').write_text('No visual sample; no semantic fields or approvals.\n')
            path=folder/'visual-fields.json';path.write_text(json.dumps({'state':'not_applicable'}))
            return {'status':'not_applicable','reason':'no_visual_evidence','artifacts':(self.artifact(path),)}
        if manifest.get('status')!='succeeded' or not frames:raise ModelFailure('evidence_unavailable')
        # Only the first actual sampled frame. No claim that an entire video was observed.
        row=frames[0];ref=ArtifactRef(**{k:row['artifact'][k] for k in ('kind','ref','sha256')})
        if ref not in task.input_refs or row['source_sha256']!=task.input_refs[0].sha256:raise ModelFailure('frame_binding_mismatch')
        frame=FrameInput(artifact=ref,source_sha256=row['source_sha256'],normalized_seconds=row['normalized_seconds'])
        output=_directory(self.router.workspace,task.output_prefix.rstrip('/'))
        (output/'README.md').write_text('Private local VLM subject/style/keyword draft and sampled-frame evidence. No medical, license, identity or synonym publication.\n',encoding='utf8')
        key=self.cache_key(task.task_id,frame,lease.config)
        report=self.cache.pop(key,None)
        if report is not None:
            with self.router.files.open_read(ref.ref) as stream:raw=stream.read(16*1024**2+1)
            if len(raw)>16*1024**2 or sha256(raw).hexdigest()!=ref.sha256:raise ModelFailure('frame_integrity')
            report=dict(report,cache_reused=True,cache_reused_for_task=task.task_id)
        else:
            request={'workspace':str(self.router.workspace),'model_root':str(self.registry.root.resolve()),'model_revision':self.registry.revision,
                'source_sha256':frame.source_sha256,'frame':frame.model_dump(mode='json'),'options':lease.config,'prompt_version':PROMPT_VERSION,
                'executor_pid':os.getpid(),'task_id':task.task_id,'inference_batch_id':f'{task.task_id}:{task.lease_epoch}'}
            candidates=self.queued_candidates(task,lease.config)
            if candidates:request['prefetch']=[r for _,r in candidates]
            deadline=time.monotonic()+task.limits.timeout_seconds
            response=self.invoke(request,output,task,stopped,deadline=deadline)
            if not response.get('ok') and response.get('error_code')=='oom' and candidates:
                # Explicitly reduce the microbatch once; the first process has stopped.
                self.batch_size=1;request.pop('prefetch',None)
                response=self.invoke(request,output,task,stopped,suffix='-single-retry',deadline=deadline)
                if response.get('ok'):response['data']['microbatch_fallback']='oom_to_single'
            if not response.get('ok'):raise ModelFailure(response.get('error_code','semantic_process_failed'))
            report=response['data']
            expected={r['task_id']:(cache_key,r) for cache_key,r in candidates}
            for entry in response.get('prefetched',[]):
                value=expected.get(entry.get('task_id'))
                if value is None:continue
                cache_key,source=value;candidate=entry.get('report',{})
                if self.bound(candidate,source['frame']):
                    try:Caption.model_validate_json(json.dumps(candidate['caption']))
                    except ValueError:continue
                    self.cache[cache_key]=candidate
            while len(self.cache)>16:self.cache.popitem(last=False)
        if not self.bound(report,frame.model_dump(mode='json')):raise ModelFailure('semantic_result_binding')
        caption=Caption.model_validate_json(json.dumps(report['caption']))
        path=output/'visual-fields.json';path.write_text(json.dumps(report,sort_keys=True,allow_nan=False),encoding='utf8')
        evidence=Evidence(evidence_id='visual-fields-frame',kind='image' if frame.normalized_seconds is None else 'frame',
            source_sha256=frame.source_sha256,artifact=ref,start_seconds=frame.normalized_seconds)
        return {'status':'succeeded','artifacts':(self.artifact(path),),'evidence':(evidence,),
            'suggestions':suggestions(caption,task,evidence.evidence_id)}

    def cache_key(self,task_id,frame,config):
        from media_tag.catalog.service import encode
        normalized=SemanticOptions.model_validate(config).model_dump()
        return sha256(encode([task_id,frame.model_dump(mode='json'),normalized,self.registry.revision,PROMPT_VERSION]).encode()).hexdigest()

    def bound(self,report,frame):
        return (report.get('source_sha256')==frame['source_sha256'] and report.get('frame')==frame
            and report.get('model_revision')==self.registry.revision and report.get('prompt_version')==PROMPT_VERSION)

    def queued_candidates(self,task,config):
        if self.batch_size<=1:return []
        result=[];normalized=SemanticOptions.model_validate(config).model_dump()
        with self.router.database.transaction() as c:
            owner=c.execute('SELECT owner_id FROM router_jobs WHERE job_id=?',(task.job_id,)).fetchone()[0]
            rows=c.execute('''SELECT t.* FROM router_tasks t JOIN router_jobs j USING(job_id)
                WHERE j.owner_id=? AND j.cancelled=0 AND j.paused=0 AND t.active=1 AND t.state IN ('queued','retry_wait')
                AND t.ready_at<=? AND json_extract(t.spec_json,'$.stage')='visual_fields'
                ORDER BY j.created_at,t.rowid LIMIT 16''',(owner,self.router.clock())).fetchall()
            for row in rows:
                if len(result)>=self.batch_size-1:break
                try:
                    step=StepSpec.model_validate_json(row['spec_json'])
                    if step.versions.model_revision!=self.registry.revision or step.versions.preprocess_version!=PREPROCESS or step.target_fields!=FIELDS or SemanticOptions.model_validate(step.config).model_dump()!=normalized:continue
                    job=c.execute('SELECT * FROM router_jobs WHERE job_id=?',(row['job_id'],)).fetchone()
                    source=self.router._task_source(c,job,step)
                    state,refs=self.router._parents(c,row)
                    if state!='ready':continue
                    manifests=[r for r in refs if r.kind=='evidence_manifest']
                    if len(manifests)!=1:continue
                    manifest=read_json(self.router.files,manifests[0])
                    if manifest.get('status')!='succeeded' or not manifest.get('frames'):continue
                    sample=manifest['frames'][0];artifact=ArtifactRef(**{k:sample['artifact'][k] for k in ('kind','ref','sha256')})
                    if artifact not in refs or sample['source_sha256']!=source['blob_sha256']:continue
                    frame=FrameInput(artifact=artifact,source_sha256=sample['source_sha256'],normalized_seconds=sample['normalized_seconds'])
                    request={'task_id':row['task_id'],'source_sha256':frame.source_sha256,'frame':frame.model_dump(mode='json')}
                    result.append((self.cache_key(row['task_id'],frame,step.config),request))
                except Exception:continue  # Optional lookahead never invalidates the leased source.
        return result

    def invoke(self,request,output,task,stopped,*,suffix='',deadline=None):
        request_path=output/('request'+suffix+'.json');response_path=output/('response'+suffix+'.json')
        request_path.write_text(json.dumps(request,sort_keys=True),encoding='utf8')
        env=os.environ.copy();env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='1',PYTHONUTF8='1')
        started=time.monotonic()
        with (output/('native'+suffix+'.stdout.log')).open('wb') as stdout,(output/('native'+suffix+'.stderr.log')).open('wb') as stderr:
            process=subprocess.Popen([sys.executable,'-m','media_tag.semantic.runner',str(request_path),str(response_path)],env=env,
                stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                while process.poll() is None:
                    if stopped.wait(.1):raise ModelFailure('cancelled')
                    if time.monotonic()-started>=task.limits.timeout_seconds or deadline is not None and time.monotonic()>=deadline:raise ModelFailure('semantic_timeout')
                    if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size>2*1024**2:raise ModelFailure('model_log_budget')
            finally:
                if process.poll() is None:process.kill()
                process.wait()
        if not response_path.is_file() or response_path.stat().st_size>512*1024:raise ModelFailure('semantic_process_failed')
        return json.loads(response_path.read_text(encoding='utf8'))

    def artifact(self,path):return ArtifactRef(kind='visual_fields_report',ref=path.relative_to(self.router.workspace).as_posix(),sha256=sha256(path.read_bytes()).hexdigest())
