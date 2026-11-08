"""Asset-bound terminology Worker: immutable report only, never effective labels."""
from hashlib import sha256
import json
import time
from media_tag.catalog import Principal
from media_tag.contracts.models import ArtifactRef,ErrorDetail
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StageDefinition
from media_tag.terminology.resolve.models import ResolveTask,PREPROCESS_VERSION,MATCH_VERSION
from media_tag.workers.evidence import _directory


def terminology_stages(registry):
    return {'terminology_resolve':StageDefinition(ResolveTask,f'{MATCH_VERSION}:{registry.revision}',
        metadata_sensitive=True,required_model_revision=registry.revision,required_preprocess_version=PREPROCESS_VERSION,
        required_gpu_slots=0,allowed_target_fields=())}


class TerminologyHandlers:
    def __init__(self,resolver):self.resolver=resolver;self.router=resolver.terminology.router

    def _origin(self,lease,request,asset):
        if request.evidence_origin=='human_input':
            if request.evidence_ref is not None:raise ModelFailure('text_origin_mismatch')
            return 'explicit_human_text_not_media_evidence',None
        if request.evidence_origin=='old_filename':
            if request.evidence_ref is not None or request.query!=asset.name:raise ModelFailure('text_origin_mismatch')
            return 'registered_display_name_only_not_medical_evidence',None
        if request.evidence_origin=='prior_quote':
            values=[v for field in asset.effective_values if field.field=='曾用原话' for v in (field.value.values or ())]
            if request.evidence_ref is not None or request.query not in values:raise ModelFailure('text_origin_mismatch')
            return 'existing_human_value_not_medical_evidence',None
        if request.evidence_origin!='ocr':raise ModelFailure('text_origin_unavailable')
        ref=request.evidence_ref
        if ref is None:
            reports=[r for r in lease.task.input_refs if r.kind=='model_report']
            if len(reports)==1:ref=reports[0]
        if ref is None or ref.kind!='model_report' or ref not in lease.task.input_refs:raise ModelFailure('text_origin_mismatch')
        with self.router.files.open_read(ref.ref) as stream:raw=stream.read(8*1024**2+1)
        if len(raw)>8*1024**2 or sha256(raw).hexdigest()!=ref.sha256:raise ModelFailure('text_origin_integrity')
        report=json.loads(raw)
        if report.get('kind')!='ocr' or report.get('source_sha256')!=asset.sha256:raise ModelFailure('text_origin_mismatch')
        lines=[line['text'] for frame in report.get('frames',[]) for line in frame.get('observation',{}).get('lines',[])]
        if request.query not in (*lines,'\n'.join(lines)):raise ModelFailure('text_origin_mismatch')
        return 'verified_parent_ocr_report_text_not_visual_medical_confirmation',ref

    def __call__(self,lease,stopped):
        task=lease.task;started=time.monotonic()
        parent=_directory(self.router.workspace,task.output_prefix.rstrip('/').rsplit('/',1)[0])
        output=parent/f'attempt-{task.attempt}';output.mkdir()
        (output/'README.md').write_text('Private D14 terminology suggestions. No effective labels or vocabulary publication.\n',encoding='utf-8')
        (output/'task.json').write_text(task.model_dump_json(indent=2),encoding='utf-8')
        (output/'config.json').write_text(json.dumps(lease.config,ensure_ascii=True,sort_keys=True),encoding='utf-8')
        try:
            config=ResolveTask.model_validate_json(json.dumps(lease.config))
            with self.router.database.transaction() as c:
                job=c.execute('SELECT owner_id FROM router_jobs WHERE job_id=?',(task.job_id,)).fetchone()
            if not job:raise ModelFailure('text_job_unavailable')
            actor=Principal(actor_id=job['owner_id'])
            asset=self.router.catalog.get_asset(task.asset_id,principal=actor)
            if asset.sha256!=task.input_refs[0].sha256 or asset.revision!=task.asset_revision:raise ModelFailure('text_source_revision')
            origin,source_ref=self._origin(lease,config.request,asset)
            with self.router.database.transaction() as c:
                _,_,versions=self.resolver.indices.pair(c,config.taxonomy_release_id,config.lexicon_release_id)
            if (task.versions.taxonomy_version,task.versions.lexicon_version,task.versions.model_revision,task.versions.preprocess_version)!=(
                versions['taxonomy_version'],versions['lexicon_version'],self.resolver.registry.revision,PREPROCESS_VERSION):
                raise ModelFailure('text_task_version_mismatch')
            if stopped.is_set():raise ModelFailure('cancelled')
            report=self.resolver.resolve(config.request,principal=actor,pinned=config,timeout_seconds=task.limits.timeout_seconds,stopped=stopped)
            if stopped.is_set():raise ModelFailure('cancelled')
            report.update(asset_id=asset.asset_id,source_sha256=asset.sha256,asset_revision=asset.revision,input_fingerprint=task.input_fingerprint,
                          origin_validation=origin,evidence_ref=source_ref.model_dump(mode='json') if source_ref else None)
            path=output/'terminology.json';path.write_text(json.dumps(report,ensure_ascii=True,sort_keys=True,allow_nan=False),encoding='utf-8')
            result={'status':'partial' if report['state']=='degraded' else 'succeeded',
                    'artifacts':(ArtifactRef(kind='terminology_report',ref=path.relative_to(self.router.workspace).as_posix(),sha256=sha256(path.read_bytes()).hexdigest()),),
                    'peak_memory_bytes':getattr(self.resolver.encoder,'stats',{}).get('peak_process_bytes')}
            if report['state']=='degraded':result['errors']=(ErrorDetail(code=report['errors'][-1],message='Term resolution degraded; original text preserved'),)
            return result
        except Exception as exc:
            (output/'failure.json').write_text(json.dumps({'code':exc.code if isinstance(exc,ModelFailure) else 'terminology_execution_failed',
                'elapsed_seconds':time.monotonic()-started}),encoding='utf-8')
            raise
        finally:self.resolver.close()
