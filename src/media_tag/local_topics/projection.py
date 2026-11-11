from hashlib import sha256
import json
import os
from pathlib import Path

from media_tag.analysis.contracts import AnalysisProducer, FieldAssessment, TOPIC_FIELDS
from media_tag.contracts.models import FieldValue, TaskEnvelope, WorkerResult, validate_result_for_task
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StepSpec
from media_tag.semantic.registry import SemanticRegistry
from media_tag.storage import StorageError

from .contracts import Generation, LocalTopicOptions, RouterTopicReport
from .parser import assess
from .prompt import PROMPT_VERSION, prompts
from .runtime import read_json, suggestions
from .service import PREPROCESS_VERSION, prepare_image


def verified(router, row, asset):
    """One current, active, released attempt. Never accepts synthetic model results."""
    task = TaskEnvelope.model_validate_json(row['envelope_json'])
    result = WorkerResult.model_validate_json(row['result_json'])
    spec = StepSpec.model_validate_json(row['spec_json'])
    options = LocalTopicOptions.model_validate_json(json.dumps(spec.config))
    with router.database.transaction() as connection:
        current = connection.execute('''SELECT t.active,t.state,t.lease_epoch,t.job_id,t.spec_json,a.envelope_json,a.result_json,a.released_at,
            j.cancelled FROM router_tasks t JOIN router_jobs j ON j.job_id=t.job_id
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.task_id=?''',
            (task.task_id,)).fetchone()
    if (current is None or current['cancelled'] or not current['active'] or current['state'] != 'succeeded'
            or current['released_at'] is None or current['lease_epoch'] != task.lease_epoch
            or current['envelope_json'] != row['envelope_json'] or current['result_json'] != row['result_json']
            or current['spec_json'] != row['spec_json']
            or current['job_id'] != task.job_id or row['task_id'] != task.task_id or row['job_id'] != task.job_id
            or task.stage != 'local_topics' or spec.stage != task.stage or task.asset_id != asset['asset_id']
            or task.input_refs[0].sha256 != asset['blob_sha256'] or result.status != 'succeeded'
            or spec.versions != task.versions or spec.target_fields != task.target_fields
            or not task.target_fields or not set(task.target_fields) <= set(TOPIC_FIELDS)):
        raise ValueError('local_topic_current_attempt_changed')
    validate_result_for_task(task, result)
    limits = {'local_topic_report': 1024**2, 'local_topic_raw_report': 1024**2,
        'local_topic_source': options.max_input_bytes, 'local_topic_native_input': 16*1024**2,
        'local_topic_model_image': 16*1024**2}
    if len(result.artifacts) != len(limits) or {ref.kind for ref in result.artifacts} != set(limits):
        raise ValueError('exact_local_topic_artifacts_required')
    for artifact in result.artifacts:
        if not artifact.ref.startswith(task.output_prefix):
            raise ValueError('local_topic_attempt_output_required')
        maximum = limits[artifact.kind]
        with router.files.open_read(artifact.ref) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > maximum:
                raise ValueError('local_topic_artifact_budget')
            digest, count = sha256(), 0
            while chunk := stream.read(min(1024**2, maximum-count+1)):
                count += len(chunk)
                if count > maximum:
                    raise ValueError('local_topic_artifact_budget')
                digest.update(chunk)
            if digest.hexdigest() != artifact.sha256 or os.fstat(stream.fileno()).st_size != size:
                raise ValueError('local_topic_artifact_integrity')
    refs = [ref for ref in result.artifacts if ref.kind == 'local_topic_report']
    if len(refs) != 1:raise ValueError('one_local_topic_report_required')
    report = RouterTopicReport.model_validate_json(json.dumps(read_json(router,refs[0])))
    local = report.result
    document = local.document
    policy = getattr(router.catalog,'review_policy',None)
    expected_revision = SemanticRegistry(Path('.')).revision
    if (policy is None or options.policy_digest != policy.digest or local.policy_digest != policy.digest
            or options.model_revision != expected_revision or local.model_revision != expected_revision
            or task.versions.model_revision != expected_revision or task.versions.rules_version != 'sha256:'+policy.digest
            or task.versions.preprocess_version != PREPROCESS_VERSION or task.versions.taxonomy_version != policy.config.taxonomy_version
            or task.limits.gpu_slots != 1 or spec.prompt_version != PROMPT_VERSION or local.prompt_version != PROMPT_VERSION
            or report.task_id != task.task_id or report.lease_epoch != task.lease_epoch
            or report.input_fingerprint != task.input_fingerprint or report.versions != task.versions
            or local.backend != 'local_qwen' or document.asset_id != asset['asset_id']
            or document.asset_revision != task.asset_revision or document.content_sha256 != asset['blob_sha256']
            or document.content_kind != 'image' or result.evidence != document.evidence
            or result.suggestions != suggestions(document,task)
            or any(ref not in result.artifacts for ref in (report.source,report.native_input,local.image,local.raw_report))
            or report.source.kind != 'local_topic_source' or report.source.sha256 != asset['blob_sha256']
            or report.native_input.kind != 'local_topic_native_input' or report.native_input.sha256 != local.image.sha256
            or local.image.kind != 'local_topic_model_image' or local.raw_report.kind != 'local_topic_raw_report'
            or len(document.evidence) != 1 or document.evidence[0].kind != 'image'
            or document.evidence[0].artifact != local.image
            or document.evidence[0].source_sha256 != asset['blob_sha256']):
        raise ValueError('local_topic_report_binding_changed')
    raw = read_json(router,local.raw_report)
    generation = Generation.model_validate_json(json.dumps(raw['generation']))
    if (raw['schema_version'] != 'ct-local-topics-raw-v1' or raw['input_content_kind'] != 'image'
            or any(raw[name] is not False for name in ('medical_confirmation','privacy_confirmation','effective_values_written'))
            or generation.backend != 'local_qwen' or generation.model_revision != expected_revision
            or generation.prompt_version != PROMPT_VERSION or generation.image_sha256 != local.image.sha256
            or raw['source_sha256'] != asset['blob_sha256'] or raw['image'] != local.image.model_dump(mode='json')
            or raw['prompt_version'] != PROMPT_VERSION or raw['policy_digest'] != policy.digest
            or raw['taxonomy_version'] != policy.config.taxonomy_version or raw['preprocess_version'] != PREPROCESS_VERSION
            or raw['options'] != options.model_dump(mode='json')
            or raw['field_prompts'] != list(prompts(policy,options))):
        raise ValueError('local_topic_raw_report_binding_changed')
    recomputed = tuple(assess(answer,field,policy=policy,versions=task.versions,evidence_id=document.evidence[0].evidence_id)
        for field,answer in zip(TOPIC_FIELDS,generation.answers,strict=True))
    if recomputed != document.fields:
        raise ValueError('local_topic_normalization_changed')
    with router.files.open_read(report.source.ref) as stream:
        source_bytes = stream.read(options.max_input_bytes+1)
    if sha256(prepare_image(source_bytes,options)).hexdigest() != local.image.sha256:
        raise ValueError('local_topic_image_not_normalized_from_source')
    return task,result,report,refs[0]


def merge(router, connection, asset, kind, job, fields, evidence, producers):
    from media_tag.analysis.scopes import latest_topic_fields
    winners = latest_topic_fields(connection, job_id=job['job_id'])
    states, claimed = {}, set()
    evidence, producers = list(evidence),list(producers)
    rows = connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at
        FROM router_tasks t LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
        WHERE t.job_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')='local_topics'
        ORDER BY t.rowid DESC''',(job['job_id'],)).fetchall()
    for row in rows:
        spec = StepSpec.model_validate_json(row['spec_json'])
        selected = {field for field in spec.target_fields if winners.get(field) == row['task_id']}-claimed
        if not selected:continue
        claimed |= selected
        for name in selected:
            fields[name] = FieldAssessment(field=name,value=FieldValue(state='unknown',reason='local_topic_task_pending'),
                next_step='local',reason='local_topic_task_pending')
        states[row['step_key']] = row['state']
        if row['state'] != 'succeeded' or row['released_at'] is None or not row['result_json']:
            if row['state'] in ('failed','needs_attention','cancelled'):
                for name in selected:
                    fields[name] = FieldAssessment(field=name,value=FieldValue(state='failed',reason='local_topic_task_failed'),
                        next_step='local',reason='local_topic_task_failed')
            continue
        try:
            if kind != 'image':raise ValueError('local_topic_static_only')
            task,result,report,ref = verified(router,row,asset)
            document = report.result.document
            producer_id = f'{task.task_id}.{task.lease_epoch}'
            mapping = {item.evidence_id:'local-topic-'+sha256(f'{producer_id}:{item.evidence_id}'.encode()).hexdigest()[:32]
                       for item in document.evidence}
            adopted = tuple(item.model_copy(update={'evidence_id':mapping[item.evidence_id]}) for item in document.evidence)
            for item in document.fields:
                if item.field in selected:
                    fields[item.field] = item.model_copy(update={'evidence_ids':tuple(mapping[key] for key in item.evidence_ids),
                        'producer_ids':(producer_id,)})
            evidence.extend(adopted)
            producers.append(AnalysisProducer(producer_id=producer_id,task_id=task.task_id,stage=task.stage,
                lease_epoch=task.lease_epoch,source_sha256=asset['blob_sha256'],input_fingerprint=task.input_fingerprint,
                result_sha256=sha256(row['result_json'].encode()).hexdigest(),report=ref,versions=task.versions,
                prompt_version=PROMPT_VERSION,evidence_ids=tuple(item.evidence_id for item in adopted)))
            states[row['step_key']] = 'verified_local_topic_draft'
        except (StorageError,OSError,ValueError,KeyError,TypeError,StopIteration,ModelFailure):
            for name in selected:
                fields[name] = FieldAssessment(field=name,value=FieldValue(state='failed',reason='local_topic_evidence_unavailable'),
                    next_step='evidence',reason='local_topic_evidence_unavailable')
            states[row['step_key']] = 'unavailable'
    return fields,tuple(evidence),tuple(producers),states
