"""Append missing configured fields to a previously tagged asset's analysis job."""
from uuid import uuid4

from media_tag.catalog import RevisionConflict
from media_tag.export.service import ExportService
from media_tag.local_topics.runtime import steps_for_fields
from media_tag.models.contracts import ModelFailure
from media_tag.precision.runtime import provider_for
from media_tag.precision.service import PrecisionSubmission, request_provider
from .probe import semantic_content_kind
from .scopes import latest_topic_fields


def enqueue_missing(service, reviews, selection, expected_revision, principal):
    asset_id = selection['asset_id']
    names = tuple(reviews.policy.tag_fields.guidance)
    with service.database.transaction() as connection:
        asset = service.catalog._authorize(connection, asset_id, principal, 'edit')
        intent = connection.execute("""SELECT i.content_kind,r.job_id FROM analysis_intents i
            LEFT JOIN router_requests r ON r.owner_id=i.requested_by AND r.request_id=i.intent_id
            WHERE i.asset_id=? AND i.requested_by=? AND i.source_sha256=?""",
            (asset_id, principal.actor_id, asset['blob_sha256'])).fetchone()
        bound = intent['job_id'] if intent else None
        kind = intent['content_kind'] if intent else None
        if not kind and names:
            kind = semantic_content_kind(ExportService(reviews)._probe(connection, asset))

    with service.database.transaction(write=True) as connection:
        asset = service.catalog._authorize(connection, asset_id, principal, 'edit')
        if (asset['owner_id'] != principal.actor_id or service.files._gate(connection, asset)
                or asset['blob_sha256'] != selection['source_sha256']
                or (expected_revision is not None and asset['revision'] != expected_revision)):
            raise RevisionConflict('selected asset changed')
        covered = set(latest_topic_fields(connection, asset_id=asset_id))
        covered.update(row[0] for row in connection.execute(
            'SELECT field FROM effective_values WHERE asset_id=?', (asset_id,)))
        missing = tuple(name for name in names if name not in covered)
        if not missing:
            return {'state': 'no_missing_fields', 'fields': []}
        if bound:
            job = service.router._job_auth(connection, bound, principal)
        else:
            # Older library jobs precede analysis_intents. A later crop or retime
            # job on the same asset does not carry the verified probe dependency.
            job = connection.execute("""SELECT j.* FROM router_jobs j WHERE j.asset_id=? AND j.owner_id=?
                AND j.cancelled=0 AND EXISTS (SELECT 1 FROM router_tasks t WHERE t.job_id=j.job_id
                    AND t.active=1 AND t.step_key='probe' AND json_extract(t.spec_json,'$.stage')='probe')
                ORDER BY j.rowid DESC LIMIT 1""", (asset_id, principal.actor_id)).fetchone()
        if job is None or job['paused'] or job['cancelled'] or job['asset_id'] != asset_id:
            return {'state': 'awaiting_active_analysis_job', 'fields': list(missing)}
        job_id = job['job_id']
        if kind == 'image' and 'local_topics' in service.router.stages:
            patches = tuple(step for step in steps_for_fields(service.router) if step.target_fields[0] in missing)
            service.router._replan_tx(connection, job_id, patches, expected_revision=job['revision'], principal=principal)
            return {'state': 'queued', 'fields': list(missing), 'job_id': job_id}

    if not service.options.enabled or service.precision_deployment is None:
        raise ModelFailure('precision_provider_unavailable')
    provider_for(service.precision_deployment, kind)
    request = PrecisionSubmission(request_id='backfill-'+uuid4().hex, expected_asset_revision=asset['revision'],
        source_sha256=asset['blob_sha256'], expected_job_revision=job['revision'], fields=missing)
    request_provider(service, service.precision_deployment, asset_id, request, principal=principal,
                     stage='precision', legacy_job=None if bound else (job_id, kind))
    return {'state': 'queued', 'fields': list(missing), 'job_id': job_id}
