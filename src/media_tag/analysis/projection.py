"""Build ten-field drafts only from current verified results; never write effective values."""
from hashlib import sha256
import json

from media_tag.contracts.models import FieldValue, TaskEnvelope, WorkerResult
from media_tag.drafts.service import DraftIndex
from media_tag.semantic.contracts import FIELDS
from media_tag.storage import StorageError

from .contracts import AnalysisDocument, AnalysisProducer, FieldAssessment, TOPIC_FIELDS
from .routing import route_local_visual


def pending(field, reason, *, next_step='local', state='unknown'):
    return FieldAssessment(field=field, value=FieldValue(state=state, reason=reason),
                           next_step=next_step, reason=reason)


class TopicProjection:
    def __init__(self, router):
        self.router = router
        self.drafts = DraftIndex(router)
        self.precision_states = {}
        self.local_topic_states = {}

    def build(self, connection, asset, content_kind, job=None):
        fields = {field: pending(field, 'local_topic_capability_not_configured') for field in TOPIC_FIELDS}
        evidence, producers = (), ()
        stage_state = 'not_requested'
        if content_kind == 'audio':
            fields = {field: pending(field, 'audio_semantic_provider_not_configured', next_step='provider')
                      for field in TOPIC_FIELDS}
            for field in ('画面主体', '表现形式'):
                fields[field] = FieldAssessment(field=field,
                    value=FieldValue(state='not_applicable', reason='pure_audio_has_no_visual_content'),
                    next_step='none', reason='pure_audio_has_no_visual_content')
        elif job is not None and not job['cancelled']:
            # Scope to this request's bound job. A success in a different job is not
            # a fallback for this job's newer pending, cancelled or damaged attempt.
            row = connection.execute('''SELECT t.task_id,t.state,a.envelope_json,a.result_json,a.released_at
                FROM router_tasks t LEFT JOIN router_attempts a
                ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                WHERE t.job_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')='visual_fields'
                ORDER BY t.rowid DESC LIMIT 1''', (job['job_id'],)).fetchone()
            stage_state = 'not_requested' if row is None else row['state']
            if row is not None:
                for field in FIELDS:
                    fields[field] = pending(field, 'local_visual_task_pending')
                if row['state'] == 'succeeded' and row['released_at'] is None:
                    stage_state = 'awaiting_release'
                if row['state'] in ('failed', 'needs_attention'):
                    for field in FIELDS:
                        fields[field] = pending(field, 'local_visual_task_failed', state='failed')
                elif row['state'] == 'succeeded' and row['released_at'] is not None and row['result_json']:
                    try:
                        task = TaskEnvelope.model_validate_json(row['envelope_json'])
                        result = WorkerResult.model_validate_json(row['result_json'])
                        if (task.task_id != row['task_id'] or task.job_id != job['job_id']
                                or task.stage != 'visual_fields' or task.asset_id != asset['asset_id']
                                or task.input_refs[0].sha256 != asset['blob_sha256'] or result.status != 'succeeded'):
                            raise ValueError('current successful visual source required')
                        ref, report = self.drafts._report(task, result)
                        projected = self.drafts._project(task, result, ref, report)
                        if set(projected['field_states']) != set(FIELDS):
                            raise ValueError('four original local visual fields required')
                        evidence = tuple(item for item in result.evidence
                            if item.evidence_id == projected['provenance']['evidence_id'])
                        if len(evidence) != 1:
                            raise ValueError('one actual caption input evidence required')
                        producer = AnalysisProducer(producer_id=f'{task.task_id}.{task.lease_epoch}',
                            task_id=task.task_id, stage=task.stage, lease_epoch=task.lease_epoch,
                            source_sha256=asset['blob_sha256'], input_fingerprint=task.input_fingerprint,
                            result_sha256=sha256(row['result_json'].encode()).hexdigest(), report=ref,
                            versions=task.versions, prompt_version=report['prompt_version'],
                            evidence_ids=tuple(item.evidence_id for item in evidence))
                        producers = (producer,)
                        for field in FIELDS:
                            value = FieldValue.model_validate_json(json.dumps(projected['field_states'][field]))
                            action, reason = route_local_visual(value, content_kind)
                            fields[field] = FieldAssessment(field=field, value=value, next_step=action,
                                reason=reason, origin='local', versions=task.versions,
                                evidence_ids=tuple(item.evidence_id for item in evidence),
                                producer_ids=(producer.producer_id,))
                        # Validate modality/evidence constraints before treating the result as usable.
                        self._document(asset, content_kind, fields, evidence, producers)
                        stage_state = 'verified_local_draft'
                    except (StorageError, OSError, ValueError, KeyError, TypeError, StopIteration):
                        evidence, producers = (), ()
                        for field in FIELDS:
                            fields[field] = pending(field, 'local_visual_evidence_unavailable',
                                state='failed', next_step='evidence')
                        stage_state = 'unavailable'
        elif job is not None and job['cancelled']:
            stage_state = 'cancelled'
            for field in FIELDS:
                fields[field] = pending(field, 'local_visual_job_cancelled')
        if job is not None and not job['cancelled']:
            from media_tag.local_topics.projection import merge as merge_local_topics
            fields, evidence, producers, self.local_topic_states = merge_local_topics(self.router, connection, asset,
                content_kind, job, fields, evidence, producers)
            from media_tag.precision.projection import merge
            fields, evidence, producers, self.precision_states = merge(self.router, connection, asset, content_kind, job,
                fields, evidence, producers)
        document = self._document(asset, content_kind, fields, evidence, producers)
        return document, stage_state

    @staticmethod
    def _document(asset, content_kind, fields, evidence, producers):
        document = AnalysisDocument(analysis_id='pending-snapshot', asset_id=asset['asset_id'],
            asset_revision=asset['revision'], content_sha256=asset['blob_sha256'], content_kind=content_kind,
            fields=tuple(fields[field] for field in TOPIC_FIELDS), evidence=evidence, producers=producers)
        digest = sha256(document.model_dump_json(exclude={'analysis_id'}).encode()).hexdigest()
        return document.model_copy(update={'analysis_id': f'analysis-{digest}'})

    @staticmethod
    def public(document, stage_state):
        """Explicit evidence/report summaries keep all private artifact paths server-side."""
        payload = document.model_dump(mode='json', exclude={'evidence', 'producers', 'fields'})
        payload['fields'] = [{**field.model_dump(mode='json'),
            'requires_field_review': field.requires_field_review} for field in document.fields]
        payload['evidence'] = [{'evidence_id': item.evidence_id, 'kind': item.kind,
            'source_sha256': item.source_sha256, 'artifact_sha256': item.artifact.sha256,
            'start_seconds': item.start_seconds, 'end_seconds': item.end_seconds} for item in document.evidence]
        payload['producers'] = [{**item.model_dump(mode='json', exclude={'report'}),
            'report_sha256': item.report.sha256} for item in document.producers]
        payload.update(analysis_complete=document.analysis_complete, local_visual_state=stage_state,
                       state='ai_draft', precision_exemption_calibrated=False,
                       schema_version='ct-topic-analysis-view-v1', document_schema_version=document.schema_version)
        return payload
