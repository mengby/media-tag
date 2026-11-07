"""Lease-bound CPU stages with private source copies and an owned process-tree job."""
from hashlib import sha256
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from media_tag.catalog import Principal
from media_tag.contracts.models import ArtifactRef
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StageDefinition
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.evidence import _directory
from media_tag.workers.probe import file_sha256
from .models import ScanTask,ScanOptions,RenderOptions,RetimeTask,TRACK_VERSION,RENDER_VERSION,RETIME_VERSION
from .processes import ProcessTree


def redaction_stages(registry,tools):
    tool_id=sha256(json.dumps(tools.identity,sort_keys=True).encode()).hexdigest()
    return {kind:StageDefinition(config,f'{version}:{registry.revision}:{tool_id}',metadata_sensitive=False,
        required_model_revision=registry.revision,required_preprocess_version=version,required_gpu_slots=0,allowed_target_fields=())
        for kind,config,version in [('face_track',ScanTask,TRACK_VERSION),('redaction_render',RenderOptions,RENDER_VERSION)]}


def retime_stages(tools):
    identity=sha256(json.dumps(tools.identity,sort_keys=True).encode()).hexdigest()
    return {'retime_render':StageDefinition(RetimeTask,f'{RETIME_VERSION}:{identity}',metadata_sensitive=False,
        required_preprocess_version=RETIME_VERSION,required_gpu_slots=0,allowed_target_fields=(),requires_no_model=True)}


class RedactionHandlers:
    def __init__(self,service):
        self.service=service; self.router=service.router

    def __call__(self,lease,stopped):
        task=lease.task; config=lease.config; router=self.router
        if stopped.is_set(): raise ModelFailure('cancelled')
        with router.database.transaction(write=True) as connection:
            attempt=connection.execute('SELECT worker_id FROM router_attempts WHERE task_id=? AND lease_epoch=?',(task.task_id,task.lease_epoch)).fetchone()
            if attempt is None or router._lease(connection,task.task_id,task.lease_epoch,Principal(actor_id=attempt[0],role='worker')) is None:
                raise ModelFailure('lease_unavailable')
            owner=connection.execute('SELECT owner_id FROM router_jobs WHERE job_id=?',(task.job_id,)).fetchone()[0]
        delegate=Principal(actor_id=owner)
        plan=self.service.variants.get_plan(config['variant_plan_id'],principal=delegate)
        if plan['recipe_hash']!=config['variant_plan_hash'] or plan['parent_asset_id']!=task.asset_id or plan['source_sha256']!=task.input_refs[0].sha256:
            raise ModelFailure('variant_plan_binding')
        recipe=plan['contract']['spec']['recipe']
        if task.versions.preprocess_version!=recipe['renderer_revision'] or task.versions.model_revision!=recipe['model_revision']:
            raise ModelFailure('variant_plan_version')
        if task.stage=='retime_render' and recipe['mask'] is not None:
            raise ModelFailure('mask_requires_redaction_renderer')
        parent=_directory(router.workspace,task.output_prefix.rstrip('/').rsplit('/',1)[0]); output=parent/f'attempt-{task.attempt}'; output.mkdir()
        (output/'README.md').write_text('Private D10/D11 transform evidence. No public static serving or automatic privacy approval.\n')
        (output/'task.json').write_text(task.model_dump_json(indent=2))
        inputs=_directory(router.workspace,f'jobs/{task.job_id}/inputs'); backend=FilesystemBackend(inputs,writable=True)
        with self.service.variants._content(task.asset_id,delegate) as stream:
            blob=backend.ingest(stream)
        if blob.sha256!=task.input_refs[0].sha256: raise ModelFailure('source_hash_mismatch')
        request={'kind':task.stage,'contract':plan['contract'],'source_root':str(inputs),'source_key':blob.object_key,
            'source_sha256':blob.sha256,'source_bytes':blob.size_bytes,'output':str(output),'executor_pid':os.getpid(),
            'model_root':str(self.service.registry.root.resolve()) if self.service.registry else None,
            'model_revision':self.service.registry.revision if self.service.registry else None,
            'tool_dir':str(self.service.variants.tools.ffmpeg.parent),'start_gate':str(output/'start.ready')}
        if task.stage in ('face_track','retime_render'):
            request['options']={k:v for k,v in config.items() if k in ScanOptions.model_fields}
        else:
            row,revision=self.service.revision(config['revision_hash'],principal=delegate)
            if row['asset_id']!=task.asset_id or recipe['mask'] is None or recipe['mask']['revision']!=config['revision_hash']:
                raise ModelFailure('mask_revision_binding')
            request['revision']=revision
            request['parameters']={k:recipe['mask'][k] for k in ('mode','strength','padding')}
        request_path=output/'request.json'; result_path=output/'response.json'
        request_path.write_text(json.dumps(request),encoding='utf-8')
        env=os.environ.copy(); env.update(PYTHONUTF8='1',OMP_NUM_THREADS='1',HF_HUB_OFFLINE='1')
        started=time.monotonic(); tree=ProcessTree(); process=None
        label='retime' if task.stage=='retime_render' else 'redaction'
        try:
            with (output/'native.stdout.log').open('wb') as stdout,(output/'native.stderr.log').open('wb') as stderr:
                process=subprocess.Popen([sys.executable,'-m','media_tag.transforms.runner',str(request_path),str(result_path)],
                    stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                tree.assign(process); (output/'start.ready').write_text('assigned\n')
                while process.poll() is None:
                    if stopped.wait(.1): raise ModelFailure('cancelled')
                    if time.monotonic()-started>=task.limits.timeout_seconds: raise ModelFailure(f'{label}_timeout')
                    if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size>2*1024**2:
                        raise ModelFailure('redaction_log_budget')
        finally:
            tree.stop_and_close()
            if process:
                if process.poll() is None: process.kill()
                process.wait()
        if not result_path.is_file() or result_path.stat().st_size>32*1024**2: raise ModelFailure('redaction_process_failed')
        response=json.loads(result_path.read_text(encoding='utf-8'))
        if not response['ok']: raise ModelFailure(response['error_code'])
        data=response['data']
        if data['source_sha256']!=blob.sha256: raise ModelFailure('source_hash_mismatch')
        target=output/('tracks.json' if task.stage=='face_track' else 'coverage.json')
        target.write_text(json.dumps(data,sort_keys=True,allow_nan=False),encoding='utf-8')
        def artifact(path,kind):
            return ArtifactRef(kind=kind,ref=path.relative_to(router.workspace).as_posix(),sha256=file_sha256(path))
        artifacts=[artifact(target,{'face_track':'face_tracks','redaction_render':'redaction_coverage','retime_render':'retime_report'}[task.stage])]
        if task.stage in ('redaction_render','retime_render'):
            video=artifact(output/('redacted.mp4' if task.stage=='redaction_render' else 'retimed.mp4'),
                           'redacted_video' if task.stage=='redaction_render' else 'retimed_video')
            if video.sha256!=data['output_sha256']: raise ModelFailure('output_hash_mismatch')
            artifacts.append(video)
        return {'status':'succeeded','artifacts':tuple(artifacts),'peak_memory_bytes':data['peak_process_bytes']}
