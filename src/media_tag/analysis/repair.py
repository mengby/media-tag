"""Explicit, idempotent repair of this request's configured analysis pipeline."""
from hashlib import sha256
import json

from pydantic import Field

from media_tag.catalog import AccessDenied, RevisionConflict
from media_tag.catalog.service import encode
from media_tag.contracts.models import ContractModel, Identifier, PositiveInt, Sha256, TaskEnvelope, WorkerResult
from media_tag.drafts.service import DraftIndex
from media_tag.models.contracts import ModelFailure
from media_tag.router.models import StepSpec
from media_tag.storage import StorageError

from .contracts import TOPIC_FIELDS, TopicField
from .intake import INTAKE_POLICY


class RepairSubmission(ContractModel):
    request_id: Identifier
    expected_asset_revision: PositiveInt
    source_sha256: Sha256
    expected_job_revision: PositiveInt
    fields: tuple[TopicField, ...] = Field(default=(), max_length=10)


def _context(service, connection, asset_id, spec, principal):
    if principal.role not in ('editor', 'admin'):
        raise AccessDenied('human editor required')
    asset = service.catalog._authorize(connection, asset_id, principal, 'edit')
    if asset['blob_sha256'] != spec.source_sha256 or service.files._gate(connection, asset):
        raise RevisionConflict('analysis source unavailable or changed')
    row = connection.execute('''SELECT i.*,r.job_id FROM analysis_intents i JOIN router_requests r
        ON r.owner_id=i.requested_by AND r.request_id=i.intent_id
        WHERE i.asset_id=? AND i.requested_by=? AND i.policy_version=?''',
        (asset_id, principal.actor_id, INTAKE_POLICY)).fetchone()
    if row is None or row['source_sha256'] != asset['blob_sha256'] or row['owner_id'] != asset['owner_id']:
        raise RevisionConflict('current bound analysis required')
    job = service.router._job_auth(connection, row['job_id'], principal)
    if job['asset_id'] != asset_id:
        raise RevisionConflict('bound analysis asset changed')
    return asset, row, job


def _receipt(connection, job_id, principal, request_id, fingerprint):
    row = connection.execute('''SELECT detail_json FROM router_events WHERE job_id=? AND actor_id=?
        AND action='analysis_repair' AND json_extract(detail_json,'$.request_id')=? ORDER BY event_id DESC LIMIT 1''',
        (job_id, principal.actor_id, request_id)).fetchone()
    if row:
        data = json.loads(row[0])
        if data['fingerprint'] != fingerprint:
            raise RevisionConflict('repair request ID reused with different input')
        return data['result'] | {'reused_request': True}


def _stage_state(service, row, desired, source_sha256, content_kind=None, asset_revision=None):
    if row is None:
        return 'missing'
    if row['state'] in ('queued', 'leased', 'running', 'retry_wait') or row['released_at'] is None and row['result_json']:
        return 'pending'
    if desired.stage == 'precision':
        from media_tag.precision.runtime import provider_for
        from media_tag.precision.contracts import PROMPT
        policy = getattr(service.catalog, 'review_policy', None)
        try:
            deployment = getattr(service, 'precision_deployment', None)
            provider = provider_for(deployment, content_kind) if deployment is not None else None
        except ModelFailure:
            provider = None
        if (provider is None or policy is None or desired.config.get('policy_digest') != policy.digest
                or desired.config.get('provider_revision') != provider.revision
                or desired.config.get('provider_id') != provider.provider_id or desired.prompt_version != PROMPT
                or desired.versions.rules_version != 'sha256:'+policy.digest
                or desired.versions.taxonomy_version != policy.config.taxonomy_version):
            return 'awaiting_current_precision_configuration'
    if StepSpec.model_validate_json(row['spec_json']) != desired:
        return 'incompatible'
    if not row['result_json']:
        return 'failed'
    try:
        task = TaskEnvelope.model_validate_json(row['envelope_json'])
        result = WorkerResult.model_validate_json(row['result_json'])
        if (task.task_id != row['task_id'] or task.job_id != row['job_id']
                or task.input_refs[0].sha256 != source_sha256 or task.stage != desired.stage):
            raise ValueError('source changed')
        if task.stage == 'visual_fields' and result.status == 'not_applicable':
            return 'unavailable'
        vocabulary = (task.stage == 'clip' and result.status == 'partial' and row['state'] == 'needs_attention'
            and result.errors and all(error.code == 'unpublished_vocabulary' for error in result.errors))
        if not vocabulary and (row['state'] != 'succeeded' or result.status not in ('succeeded', 'not_applicable')):
            return 'failed'
        if task.stage != 'similarity':
            service.router._artifacts(task, result)
        if task.stage == 'similarity':
            from media_tag.similarity.pipeline import verified as verified_similarity
            if asset_revision is not None and task.asset_revision != asset_revision:
                return 'incompatible'
            verified_similarity(service.router, row, {'asset_id': task.asset_id, 'blob_sha256': source_sha256})
        if task.stage == 'precision':
            from media_tag.precision.projection import verified
            verified(service.router, row, {'asset_id': task.asset_id, 'blob_sha256': source_sha256}, content_kind)
        if task.stage in ('ocr', 'face', 'clip', 'visual_fields') and result.status != 'not_applicable':
            index = DraftIndex(service.router)
            ref, report = index._report(task, result)
            projected = index._project(task, result, ref, report)
            if task.stage == 'visual_fields':
                sample = next(item for item in result.evidence if item.evidence_id == projected['provenance']['evidence_id'])
                if sample.kind not in ('image', 'frame') or content_kind == 'image' and sample.kind != 'image':
                    raise ValueError('caption input modality changed')
        return 'awaiting_vocabulary' if vocabulary else 'valid'
    except (StorageError, OSError, ValueError, KeyError, TypeError, StopIteration, ModelFailure):
        return 'unavailable'


def repair(service, asset_id, spec, *, principal):
    if len(set(spec.fields)) != len(spec.fields):
        raise ValueError('unique repair fields required')
    fingerprint = sha256(encode({'asset_id': asset_id, 'spec': spec.model_dump(mode='json')}).encode()).hexdigest()
    with service.database.transaction() as connection:
        asset, intent, job = _context(service, connection, asset_id, spec, principal)
        previous = _receipt(connection, job['job_id'], principal, spec.request_id, fingerprint)
        if previous:
            return previous
        if asset['revision'] != spec.expected_asset_revision or job['revision'] != spec.expected_job_revision:
            raise RevisionConflict('analysis revision changed')
        if job['cancelled'] or job['paused']:
            raise RevisionConflict('cancelled or paused analysis cannot be repaired')
        if not service.options.enabled:
            raise RevisionConflict('analysis dispatch is disabled')
        rules = connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()[0]
        desired = {step.key: step for step in service._steps(rules, intent['content_kind'])}
        rows = {row['step_key']: row for row in connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at
            FROM router_tasks t LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.job_id=? AND t.active=1''', (job['job_id'],))}
        if 'precision' in service.router.stages:
            for key, row in rows.items():
                spec_row = StepSpec.model_validate_json(row['spec_json'])
                if spec_row.stage == 'precision':
                    desired[key] = spec_row
        selected = {key for key, step in desired.items() if not spec.fields or set(step.target_fields) & set(spec.fields)}
        requested = set(spec.fields or TOPIC_FIELDS)
        inapplicable = requested & {'画面主体', '表现形式'} if intent['content_kind'] == 'audio' else set()
        uncovered = sorted(requested - inapplicable - {field for key in selected for field in desired[key].target_fields})
        def ancestors(key):
            for parent in desired[key].depends_on:
                if parent not in selected:
                    selected.add(parent)
                    ancestors(parent)
        for key in tuple(selected):
            ancestors(key)
        states = {key: _stage_state(service, rows.get(key), desired[key], asset['blob_sha256'], intent['content_kind'], asset['revision']) for key in sorted(selected)}
        patches = tuple(desired[key] for key, state in states.items() if state in ('missing', 'incompatible', 'failed', 'unavailable'))
        observed_tasks = {key: (row['task_id'], row['state'], row['lease_epoch']) for key, row in rows.items()}
    # Verification above is read-only and can hash media without holding a SQLite writer.
    # Recheck authority, revisions and task generations before atomic replan + receipt.
    with service.database.transaction(write=True) as connection:
        asset, current_intent, current_job = _context(service, connection, asset_id, spec, principal)
        previous = _receipt(connection, current_job['job_id'], principal, spec.request_id, fingerprint)
        if previous:
            return previous
        current_tasks = {row['step_key']: (row['task_id'], row['state'], row['lease_epoch']) for row in
            connection.execute('SELECT * FROM router_tasks WHERE job_id=? AND active=1', (current_job['job_id'],))}
        if (current_job['job_id'] != job['job_id'] or current_intent['content_kind'] != intent['content_kind']
                or asset['revision'] != spec.expected_asset_revision or current_job['revision'] != spec.expected_job_revision
                or current_job['paused'] or current_job['cancelled'] or current_tasks != observed_tasks):
            raise RevisionConflict('analysis changed during verification')
        view = (service.router._replan_tx(connection, job['job_id'], patches,
            expected_revision=spec.expected_job_revision, principal=principal) if patches else service.router._view(connection, current_job))
        replanned = sorted(task['key'] for task in view['tasks']
            if task['key'] not in rows or task['task_id'] != rows[task['key']]['task_id'])
        result = {'asset_id': asset_id, 'job_id': job['job_id'], 'accepted_job_revision': view['revision'],
            'reused_request': False, 'stage_assessments': states, 'patched_roots': [step.key for step in patches],
            'replanned_keys': replanned, 'reused_stage_keys': sorted(key for key, state in states.items()
                if state == 'valid' and key not in replanned),
            'pending_capability_fields': uncovered, 'analysis_complete': False,
            'not_applicable_fields': sorted(inapplicable),
            'effective_values_written': False, 'medical_confirmation': False}
        service.router._event(connection, job['job_id'], None, principal.actor_id, 'analysis_repair',
            {'request_id': spec.request_id, 'fingerprint': fingerprint, 'result': result})
        return result
