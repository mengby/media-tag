"""Explicit precision requests on the requester's existing analysis job."""
from hashlib import sha256
import json
from typing import Annotated

from pydantic import Field

from media_tag.analysis.contracts import TOPIC_FIELDS, TopicField
from media_tag.analysis.repair import RepairSubmission, _context
from media_tag.catalog import AccessDenied, CatalogError, RevisionConflict
from media_tag.models.contracts import ModelFailure
from media_tag.catalog.service import encode
from media_tag.contracts.models import TaskLimits, Versions
from media_tag.router.models import StepSpec

from .contracts import PROMPT, VERSION
from .runtime import provider_for
from media_tag.analysis.intake import INTAKE_POLICY


def task_key(provider, policy, kind, fields):
    return 'precision-'+sha256(encode([provider.revision, policy.digest, kind, fields]).encode()).hexdigest()[:24]


class PrecisionSubmission(RepairSubmission):
    fields: Annotated[tuple[TopicField, ...], Field(min_length=1, max_length=10)] = TOPIC_FIELDS


def request_precision(analysis, deployment, asset_id, spec, *, principal):
    if not spec.fields or len(set(spec.fields)) != len(spec.fields) or not set(spec.fields) <= set(TOPIC_FIELDS):
        raise ValueError('unique original topic fields required')
    fingerprint = sha256(encode({'asset_id': asset_id, 'request': spec.model_dump(mode='json')}).encode()).hexdigest()
    with analysis.database.transaction(write=True) as connection:
        asset, intent, job = _context(analysis, connection, asset_id, spec, principal)
        receipt = connection.execute('''SELECT detail_json FROM router_events WHERE job_id=? AND actor_id=?
            AND action='precision_request' AND json_extract(detail_json,'$.request_id')=? ORDER BY event_id DESC LIMIT 1''',
            (job['job_id'], principal.actor_id, spec.request_id)).fetchone()
        if receipt:
            previous = json.loads(receipt[0])
            if previous['fingerprint'] != fingerprint:
                raise RevisionConflict('precision request ID reused with different input')
            return previous['result'] | {'reused_request': True}
        if (asset['revision'] != spec.expected_asset_revision or job['revision'] != spec.expected_job_revision
                or job['cancelled'] or job['paused'] or intent['content_kind'] is None or not analysis.options.enabled):
            raise RevisionConflict('current active typed analysis required')
        try:
            provider = provider_for(deployment, intent['content_kind'])
        except ModelFailure:
            raise RevisionConflict('configured precision provider unavailable') from None
        policy = getattr(analysis.catalog, 'review_policy', None)
        if policy is None:
            raise RevisionConflict('published selection policy required')
        fields = tuple(field for field in TOPIC_FIELDS if field in spec.fields
            and (intent['content_kind'] != 'audio' or field not in ('画面主体', '表现形式')))
        if not fields:
            raise RevisionConflict('requested fields are not applicable to pure audio')
        probe = connection.execute('''SELECT * FROM router_tasks WHERE job_id=? AND active=1 AND step_key='probe' ''', (job['job_id'],)).fetchone()
        if probe is None or json.loads(probe['spec_json'])['stage'] != 'probe':
            raise RevisionConflict('verified probe dependency required')
        key = task_key(provider, policy, intent['content_kind'], fields)
        config = {'provider_id': provider.provider_id, 'provider_revision': provider.revision,
            'policy_digest': policy.digest, 'content_kind': intent['content_kind']}
        step = StepSpec(key=key, stage='precision', depends_on=('probe',), config=config, target_fields=fields,
            versions=Versions(rules_version='sha256:'+policy.digest, preprocess_version=VERSION,
                taxonomy_version=policy.config.taxonomy_version, model_revision=provider.revision),
            limits=TaskLimits(timeout_seconds=min(deployment.timeout_seconds, analysis.router.options.max_timeout_seconds),
                max_attempts=min(3, analysis.router.options.max_attempts), gpu_slots=1 if provider.device == 'cuda' else 0),
            prompt_version=PROMPT)
        existing = connection.execute('SELECT * FROM router_tasks WHERE job_id=? AND active=1 AND step_key=?', (job['job_id'], key)).fetchone()
        # New request IDs may retry failed work. A success is reverified on every read;
        # an explicit repair endpoint below can replace damaged successful results.
        reusable = existing is not None and StepSpec.model_validate_json(existing['spec_json']) == step and existing['state'] in ('queued', 'leased', 'running', 'retry_wait', 'succeeded')
        view = analysis.router._view(connection, job) if reusable else analysis.router._replan_tx(connection,
            job['job_id'], (step,), expected_revision=spec.expected_job_revision, principal=principal)
        task = next(item for item in view['tasks'] if item['key'] == key)
        result = {'asset_id': asset_id, 'job_id': job['job_id'], 'task_id': task['task_id'],
            'accepted_job_revision': view['revision'], 'provider_id': provider.provider_id,
            'fields': list(fields), 'reused_stage': reusable, 'reused_request': False,
            'model_capability_acceptance': False, 'medical_confirmation': False, 'effective_values_written': False}
        analysis.router._event(connection, job['job_id'], None, principal.actor_id, 'precision_request',
            {'request_id': spec.request_id, 'fingerprint': fingerprint, 'result': result})
        return result


def dispatch_pending(analysis, deployment, *, principal):
    """Opt-in escalation only after local tasks stop; failed precision work is not looped."""
    if not deployment.enabled or not deployment.auto_dispatch or not analysis.options.enabled:
        return []
    if principal.role not in ('editor', 'admin'):
        raise AccessDenied('human analysis requester required')
    policy = getattr(analysis.catalog, 'review_policy', None)
    if policy is None:
        return []
    kinds, signatures = [], []
    for kind in ('image', 'video', 'animation', 'audio'):
        try:
            provider = provider_for(deployment, kind)
        except ModelFailure:
            continue
        kinds.append({'kind': kind, 'fields': [field for field in TOPIC_FIELDS if kind != 'audio' or field not in ('画面主体', '表现形式')]})
        signatures.append(provider.revision)
    if not kinds:
        return []
    seed = sha256(encode([signatures, policy.digest]).encode()).hexdigest()
    cursor_key = 'ct_precision_scan:'+sha256(principal.actor_id.encode()).hexdigest()
    with analysis.database.transaction() as connection:
        cursor = connection.execute('SELECT value FROM catalog_metadata WHERE key=?', (cursor_key,)).fetchone()
        cursor = json.loads(cursor[0]) if cursor else {}
        position = cursor.get('position', 0) if cursor.get('seed') == seed else 0
        query = '''SELECT i.asset_id,i.rowid AS position FROM analysis_intents i
            JOIN router_requests r ON r.owner_id=i.requested_by AND r.request_id=i.intent_id
            JOIN router_jobs j ON j.job_id=r.job_id
            JOIN assets a ON a.asset_id=i.asset_id
            JOIN json_each(?) p ON json_extract(p.value,'$.kind')=i.content_kind
            WHERE i.requested_by=? AND i.policy_version=? AND i.rowid>?
            AND j.cancelled=0 AND j.paused=0 AND a.deleted_at IS NULL
            AND a.blob_sha256=i.source_sha256 AND a.owner_id=i.owner_id
            AND (?='admin' OR a.owner_id=? OR EXISTS (SELECT 1 FROM asset_permissions ap
                WHERE ap.asset_id=a.asset_id AND ap.subject_id=? AND ap.permission='edit'))
            AND EXISTS (SELECT 1 FROM router_tasks t JOIN router_attempts z ON z.task_id=t.task_id AND z.lease_epoch=t.lease_epoch
                WHERE t.job_id=j.job_id AND t.active=1 AND t.step_key='probe' AND t.state='succeeded' AND z.released_at IS NOT NULL)
            AND NOT EXISTS (SELECT 1 FROM router_tasks t LEFT JOIN router_attempts z ON z.task_id=t.task_id AND z.lease_epoch=t.lease_epoch
                WHERE t.job_id=j.job_id AND t.active=1 AND json_extract(t.spec_json,'$.stage')!='precision'
                AND (t.state NOT IN ('succeeded','failed','needs_attention','cancelled') OR t.lease_epoch>0 AND z.released_at IS NULL))
            AND EXISTS (SELECT 1 FROM json_each(p.value,'$.fields') f WHERE NOT EXISTS (
                SELECT 1 FROM router_tasks t JOIN json_each(t.spec_json,'$.target_fields') tf
                WHERE t.job_id=j.job_id AND t.active=1 AND json_extract(t.spec_json,'$.stage')='precision' AND tf.value=f.value))
            ORDER BY i.rowid LIMIT ?'''
        def scan(after):
            return connection.execute(query, (encode(kinds), principal.actor_id, INTAKE_POLICY, after,
                principal.role, principal.actor_id, principal.actor_id, deployment.dispatch_limit)).fetchall()
        rows = scan(position)
        if not rows and position:
            rows = scan(0)
    with analysis.database.transaction(write=True) as connection:
        connection.execute('INSERT INTO catalog_metadata(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (cursor_key, encode({'seed': seed, 'position': rows[-1]['position'] if rows else 0})))
    outcomes = []
    for candidate in rows:
        try:
            state = analysis.status(candidate['asset_id'], principal=principal)
            if not state.get('runtime') or state['state'] in ('paused', 'cancelled'):
                continue
            with analysis.database.transaction() as connection:
                asset = analysis.catalog._authorize(connection, candidate['asset_id'], principal, 'edit')
                provider = provider_for(deployment, state['content_kind'])
                policy = getattr(analysis.catalog, 'review_policy', None)
                if policy is None:
                    continue
                tasks = connection.execute('SELECT * FROM router_tasks WHERE job_id=? AND active=1', (state['runtime']['job_id'],)).fetchall()
                covered = {field for row in tasks if json.loads(row['spec_json'])['stage'] == 'precision'
                    for field in json.loads(row['spec_json'])['target_fields']}
                fields = tuple(field for field in TOPIC_FIELDS if field not in covered
                    and (state['content_kind'] != 'audio' or field not in ('画面主体', '表现形式')))
                if not fields:
                    continue
                locals_ = [row for row in tasks if json.loads(row['spec_json'])['stage'] != 'precision']
                if any(row['state'] not in ('succeeded', 'failed', 'needs_attention', 'cancelled') for row in locals_):
                    continue
                probe = next((row for row in tasks if row['step_key'] == 'probe'), None)
                if probe is None or probe['state'] != 'succeeded':
                    continue
            token = sha256(encode([state['runtime']['job_id'], provider.revision, policy.digest, fields]).encode()).hexdigest()[:40]
            spec = PrecisionSubmission(request_id='auto-precision-'+token, source_sha256=asset['blob_sha256'],
                expected_asset_revision=asset['revision'], expected_job_revision=state['runtime']['revision'], fields=fields)
            outcomes.append(request_precision(analysis, deployment, candidate['asset_id'], spec, principal=principal))
        except (CatalogError, ModelFailure, ValueError):
            outcomes.append({'asset_id': candidate['asset_id'], 'state': 'precision_waiting'})
    return outcomes
