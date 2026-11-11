"""Verify current precision attempts before field-by-field adoption into a draft view."""
from hashlib import sha256
import json

from media_tag.analysis.contracts import AnalysisProducer, FieldAssessment
from media_tag.contracts.models import FieldValue, TaskEnvelope, WorkerResult
from media_tag.router.models import StepSpec
from media_tag.storage import StorageError
from media_tag.models.contracts import ModelFailure

from .contracts import PROMPT, PrecisionOptions, PrecisionReport
from .runtime import duration, read_json, suggestions

WAITING = {
    'precision_provider_unavailable': 'provider', 'precision_configuration_changed': 'provider',
    'precision_verified_probe_required': 'evidence', 'precision_verified_duration_required': 'evidence',
    'precision_common_source_clock_required': 'evidence',
    'precision_track_selection_required': 'human', 'precision_duration_budget': 'provider',
    'precision_input_budget': 'provider', 'precision_request_budget': 'provider',
}


def verified(router, row, asset, kind, *, layer='precision'):
    if layer not in ('precision','local_audio'):
        raise ValueError('supported analysis layer required')
    task = TaskEnvelope.model_validate_json(row['envelope_json'])
    result = WorkerResult.model_validate_json(row['result_json'])
    spec = StepSpec.model_validate_json(row['spec_json'])
    options = PrecisionOptions.model_validate_json(json.dumps(spec.config))
    if (task.task_id != row['task_id'] or task.job_id != row['job_id'] or task.asset_id != asset['asset_id']
            or task.input_refs[0].sha256 != asset['blob_sha256'] or result.status != 'succeeded'):
        raise ValueError('precision attempt changed')
    router._artifacts(task, result)
    refs = [ref for ref in result.artifacts if ref.kind == 'precision_report']
    if len(refs) != 1:
        raise ValueError('one actual precision report required')
    report = PrecisionReport.model_validate_json(json.dumps(read_json(router, refs[0])))
    document = report.document
    policy = getattr(router.catalog, 'review_policy', None)
    if (policy is None or report.policy_digest != policy.digest or options.policy_digest != policy.digest
            or task.versions.rules_version != 'sha256:'+policy.digest or task.versions.taxonomy_version != policy.config.taxonomy_version
            or spec.prompt_version != PROMPT
            or report.provider.revision != task.versions.model_revision or options.provider_revision != report.provider.revision
            or document.content_sha256 != asset['blob_sha256'] or document.content_kind != kind
            or document.asset_id != asset['asset_id'] or document.asset_revision != task.asset_revision
            or report.analysis_layer != layer
            or task.stage != ('precision' if layer=='precision' else 'audio_topics')
            or report.request_id != ('precision-' if layer=='precision' else 'local-audio-')+task.input_fingerprint or report.prompt_version != PROMPT
            or result.evidence != document.evidence or result.suggestions != suggestions(document, task)
            or any(item.artifact not in result.artifacts for item in document.evidence)):
        raise ValueError('precision report/source/policy binding changed')
    if len(document.evidence) != 1:
        raise ValueError('whole-input precision requires one content evidence')
    sample = document.evidence[0]
    probes = [ref for ref in task.input_refs if ref.kind == 'technical_report']
    if len(probes) != 1:
        raise ValueError('one verified precision probe required')
    info = read_json(router, probes[0])
    if {'gif': 'animation'}.get(info['kind'], info['kind']) != kind:
        raise ValueError('precision input modality changed')
    from .runtime import evidence_window
    window_start,window_end = evidence_window(info, kind)
    span = None if window_start is None else window_end-window_start
    expected_kind = 'image' if span is None else 'audio_segment' if kind == 'audio' else 'video_segment'
    if (sample.artifact.kind != 'precision_input' or sample.artifact.sha256 != asset['blob_sha256']
            or sample.kind != expected_kind or sample.start_seconds != window_start
            or sample.end_seconds != window_end):
        raise ValueError('precision evidence is not the complete bounded source')
    selected = {item.field: item for item in document.fields if item.field in task.target_fields}
    if layer == 'local_audio' and report.reply is None:
        raise ValueError('local audio requires the actual provider reply')
    if report.reply is not None:
        from .assessment import assessments
        reply = report.reply
        if (reply.request_id != report.request_id or reply.source_sha256 != asset['blob_sha256']
                or reply.provider_revision != report.provider.revision
                or {item.field for item in reply.fields} != set(task.target_fields)
                or document.fields != assessments(reply, task, policy, kind, layer=layer, evidence_id=sample.evidence_id)
                or report.usage != reply.usage):
            raise ValueError('provider reply assessment changed')
    if set(selected) != set(task.target_fields):
        raise ValueError('precision requested field coverage changed')
    for item in selected.values():
        if item.versions != task.versions or item.origin != ('precision' if layer=='precision' else 'local') or item.evidence_ids != (sample.evidence_id,):
            raise ValueError('precision field producer changed')
        policy.validate(policy.asset_fields[item.field], item.value)
    return task, result, report, refs[0]


def merge(router, connection, asset, kind, job, fields, evidence, producers, *, layer='precision'):
    from media_tag.analysis.scopes import latest_topic_fields
    if layer not in ('precision','local_audio'):
        raise ValueError('supported analysis layer required')
    stage = 'precision' if layer=='precision' else 'audio_topics'
    winners = latest_topic_fields(connection, job_id=job['job_id'])
    states, claimed = {}, set()
    evidence, producers = list(evidence), list(producers)
    rows = connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at
        FROM router_tasks t LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
        WHERE t.job_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')=?
        ORDER BY t.rowid DESC''', (job['job_id'],stage)).fetchall()
    for row in rows:
        spec = StepSpec.model_validate_json(row['spec_json'])
        selected = {field for field in spec.target_fields if winners.get(field) == row['task_id']}-claimed
        if not selected:
            continue
        claimed |= selected
        for field in selected:
            fields[field] = FieldAssessment(field=field, value=FieldValue(state='unknown', reason=stage+'_task_pending'),
                next_step='precision' if layer=='precision' else 'local', reason=stage+'_task_pending')
        states[row['step_key']] = row['state']
        if row['state'] != 'succeeded' or row['released_at'] is None or not row['result_json']:
            if row['state'] in ('failed', 'needs_attention', 'cancelled'):
                for field in selected:
                    code = row['error_code']
                    reason = code if code in WAITING else stage+'_task_failed'
                    fields[field] = FieldAssessment(field=field,
                        value=FieldValue(state='unknown' if code in WAITING else 'failed', reason=reason),
                        next_step=WAITING.get(code, 'precision' if layer=='precision' else 'local'), reason=reason)
            continue
        try:
            task, result, report, ref = verified(router, row, asset, kind, layer=layer)
            document = report.document
            producer_id = f'{task.task_id}.{task.lease_epoch}'
            mapping = {item.evidence_id: stage+'-'+sha256(f'{producer_id}:{item.evidence_id}'.encode()).hexdigest()[:32]
                for item in document.evidence}
            adopted = tuple(item.model_copy(update={'evidence_id': mapping[item.evidence_id]}) for item in document.evidence)
            for item in document.fields:
                if item.field in selected:
                    fields[item.field] = item.model_copy(update={'evidence_ids': tuple(mapping[key] for key in item.evidence_ids),
                        'producer_ids': (producer_id,)})
            producers.append(AnalysisProducer(producer_id=producer_id, task_id=task.task_id, stage=task.stage,
                lease_epoch=task.lease_epoch, source_sha256=asset['blob_sha256'], input_fingerprint=task.input_fingerprint,
                result_sha256=sha256(row['result_json'].encode()).hexdigest(), report=ref, versions=task.versions,
                prompt_version=PROMPT, evidence_ids=tuple(item.evidence_id for item in adopted)))
            evidence.extend(adopted)
            states[row['step_key']] = 'verified_'+stage+'_draft'
        except (StorageError, OSError, ValueError, KeyError, TypeError, StopIteration, ModelFailure):
            for field in selected:
                fields[field] = FieldAssessment(field=field, value=FieldValue(state='failed', reason=stage+'_evidence_unavailable'),
                    next_step='evidence', reason=stage+'_evidence_unavailable')
            states[row['step_key']] = 'unavailable'
    return fields, tuple(evidence), tuple(producers), states
