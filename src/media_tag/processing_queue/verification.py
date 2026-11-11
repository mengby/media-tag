"""Bounded private artifact verification, outside catalogue write transactions."""
from hashlib import sha256
import json
import os

from media_tag.catalog.service import encode
from media_tag.cleaning.service import VERSION as CLEANING_VERSION, _validate_fixed_metadata
from media_tag.contracts.models import TaskEnvelope, WorkerResult, validate_result_for_task
from media_tag.image_processing.models import ProcessingResult as ImageResult
from media_tag.image_processing.service import VERSION as IMAGE_VERSION
from media_tag.router.models import StepSpec

from .models import CleaningTask, ImageTask, ProcessingReport

REPORT_LIMIT = 16*1024**2


def require_current_attempt(connection, row):
    """Recheck this snapshot in the caller's fresh authorization/registration transaction."""
    current = connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at,j.cancelled
        FROM router_tasks t JOIN router_jobs j ON j.job_id=t.job_id
        LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
        WHERE t.task_id=?''', (row['task_id'],)).fetchone()
    keys = ('task_id','job_id','active','state','lease_epoch','spec_json','envelope_json','result_json','released_at')
    if current is None or current['cancelled'] or not current['active'] or current['released_at'] is None or any(current[key] != row[key] for key in keys):
        raise ValueError('processing_current_attempt_changed')


def _size(router, ref, maximum, exact=None):
    with router.files.open_read(ref.ref) as stream:
        size = os.fstat(stream.fileno()).st_size
    if size <= 0 or size > maximum or exact is not None and size != exact:
        raise ValueError('processing_artifact_budget_or_size')
    return size


def _read(router, ref, maximum, *, expected_size, collect=False):
    with router.files.open_read(ref.ref) as stream:
        if os.fstat(stream.fileno()).st_size != expected_size or expected_size > maximum:
            raise ValueError('processing_artifact_size_changed')
        digest, count, parts = sha256(), 0, []
        while chunk := stream.read(min(1024**2, maximum-count+1)):
            count += len(chunk)
            if count > maximum:
                raise ValueError('processing_artifact_budget')
            digest.update(chunk)
            if collect:parts.append(chunk)
        if count != expected_size or digest.hexdigest() != ref.sha256 or os.fstat(stream.fileno()).st_size != expected_size:
            raise ValueError('processing_artifact_integrity')
    return b''.join(parts) if collect else None


def verified_result(router, tools, row, asset):
    """Return (task, result, report_or_none, report_ref_or_none).

    All bytes are bounded before reading and source/recipe/versions/core/output
    bindings are checked. Caller must recheck current actor ACL/FileGate/source
    and require_current_attempt in a fresh transaction after this long read.
    No writer transaction should be held while invoking this function.
    """
    from .runtime import implementation
    task = TaskEnvelope.model_validate_json(row['envelope_json'])
    result = WorkerResult.model_validate_json(row['result_json'])
    spec = StepSpec.model_validate_json(row['spec_json'])
    config_type = CleaningTask if spec.stage == 'cleaning_output' else ImageTask
    config = config_type.model_validate_json(encode(spec.config))
    if isinstance(config,CleaningTask) and config.recipe.source_sha256 != config.source_sha256:
        raise ValueError('processing_recipe_source_changed')
    expected_stage = 'cleaning_output' if config.operation == 'cleaning' else 'image_mask_output'
    if (task.task_id != row['task_id'] or task.job_id != row['job_id'] or task.lease_epoch != row['lease_epoch']
            or not row['active'] or row['released_at'] is None or task.asset_id != asset['asset_id']
            or task.asset_revision != config.expected_revision or task.stage != expected_stage or spec.stage != expected_stage
            or task.input_refs[0].sha256 != config.source_sha256 or asset['blob_sha256'] != config.source_sha256
            or asset['revision'] != config.expected_revision or asset['owner_id'] != config.source_owner_id
            or task.target_fields or spec.target_fields or task.limits.gpu_slots != 0 or task.versions.model_revision is not None
            or task.versions != spec.versions or task.versions.rules_version != 'sha256:'+router.catalog.contract.fingerprint
            or task.versions.preprocess_version != implementation(tools)):
        raise ValueError('processing_task_binding_changed')
    validate_result_for_task(task,result)
    if not result.artifacts and result.status == 'failed':
        return task,result,None,None
    kinds = [item.kind for item in result.artifacts]
    produced = result.status == 'succeeded'
    expected_kinds = {'processing_report','processing_output','processing_core_report'} if produced else {'processing_report'}
    if set(kinds) != expected_kinds or len(kinds) != len(expected_kinds):
        raise ValueError('exact_processing_artifacts_required')
    refs = {item.kind:item for item in result.artifacts}
    sizes = {}
    # Inspect every size before reading even the report. Huge/sparse outputs or
    # reports are rejected without an expensive generic artifact hash pass.
    for kind,ref in refs.items():
        if not ref.ref.startswith(task.output_prefix):
            raise ValueError('processing_attempt_output_required')
        sizes[kind] = _size(router,ref,config.limits.max_output_bytes if kind == 'processing_output' else REPORT_LIMIT)
    report = ProcessingReport.model_validate_json(_read(router,refs['processing_report'],REPORT_LIMIT,
        expected_size=sizes['processing_report'],collect=True))
    recipe_sha = sha256(encode(config.recipe.model_dump(mode='json')).encode()).hexdigest()
    if (report.task_id != task.task_id or report.job_id != task.job_id or report.lease_epoch != task.lease_epoch
            or report.asset_id != task.asset_id or report.asset_revision != config.expected_revision
            or report.source_sha256 != config.source_sha256 or report.input_fingerprint != task.input_fingerprint
            or report.recipe_sha256 != recipe_sha or report.operation != config.operation
            or report.renderer_version != (CLEANING_VERSION if config.operation == 'cleaning' else IMAGE_VERSION)
            or produced != (report.state == 'pending_registration_review')):
        raise ValueError('processing_report_binding_changed')
    if not produced:
        if (report.state.startswith('waiting_') and result.status != 'partial'
                or report.state == 'failed' and result.status != 'failed'):
            raise ValueError('processing_unresolved_result_changed')
        return task,result,report,refs['processing_report']
    if (row['state'] != 'succeeded' or report.output != refs['processing_output'] or report.core_report != refs['processing_core_report']
            or report.output_bytes != sizes['processing_output']):
        raise ValueError('processing_actual_output_size_or_ref_changed')
    raw = json.loads(_read(router,report.core_report,REPORT_LIMIT,expected_size=sizes['processing_core_report'],collect=True))
    if config.operation == 'cleaning':
        _validate_fixed_metadata(raw)
        if (raw.get('schema_version') != CLEANING_VERSION or raw.get('source_sha256') != config.source_sha256
                or raw.get('recipe') != config.recipe.model_dump(mode='json') or raw.get('limits') != config.limits.model_dump(mode='json')
                or raw.get('tools') != tools.identity or raw.get('content_kind') != config.recipe.content_kind
                or raw.get('job_id') != 'processing' or raw.get('output_sha256') != report.output.sha256
                or type(raw.get('output_bytes')) is not int or raw['output_bytes'] != report.output_bytes
                or raw.get('output_ref') != 'jobs/processing/cleaning/'+raw['standardized_name']):
            raise ValueError('processing_cleaning_core_binding_changed')
        summary = {'content_kind':raw['content_kind'],'duration_target_state':raw['duration_target_state'],'cache_reused':raw['cache_reused']}
        if raw['time_map']:
            summary.update(output_duration_seconds=raw['time_map']['output_duration_seconds'],speed=raw['time_map']['speed'],full_output_decode=raw['source_quality']['full_output_decode'])
    else:
        image = ImageResult.model_validate_json(encode(raw))
        if (image.source_sha256 != config.source_sha256 or image.renderer_version != IMAGE_VERSION or image.recipe != config.recipe
                or image.job_id != 'processing' or image.output_sha256 != report.output.sha256):
            raise ValueError('processing_image_core_binding_changed')
        summary = {'frame_count':image.frame_count,'animation_frame_count':image.animation_frame_count,'display_width':image.display_width,
            'display_height':image.display_height,'mask_region_count':sum(len(frame.regions) for frame in image.frames),
            'watermark_quality_status':image.watermark_quality_status}
    if report.summary != summary:
        raise ValueError('processing_summary_changed')
    _read(router,report.output,config.limits.max_output_bytes,expected_size=report.output_bytes)
    return task,result,report,refs['processing_report']
