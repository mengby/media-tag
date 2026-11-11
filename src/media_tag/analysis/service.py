"""Read authorized, requester-scoped intake state without declaring analysis complete."""

from hashlib import sha256
import json

from media_tag.catalog import AccessDenied, CatalogError
from media_tag.contracts.models import TaskEnvelope, TaskLimits, Versions, WorkerResult
from media_tag.files.service import FileService
from media_tag.router.models import JobSpec, StepSpec
from media_tag.storage import StorageError

from .contracts import TOPIC_FIELDS
from .intake import INTAKE_POLICY, KINDS, ensure_intent
from .deployment import AnalysisDeployment
from .projection import TopicProjection


class AnalysisService:
    def __init__(self, router, options=None, precision_deployment=None, audio_deployment=None):
        self.router, self.catalog, self.database = router, router.catalog, router.database
        self.files = FileService(router)
        self.options = options or AnalysisDeployment()
        self.precision_deployment = precision_deployment
        self.audio_deployment = audio_deployment
        self._dispatch_positions = {}

    def _steps(self, fingerprint, content_kind):
        versions = Versions(rules_version=f'sha256:{fingerprint}', preprocess_version='d02-cpu-v1')
        steps = (StepSpec(key='probe', stage='probe', versions=versions,
                          config={'max_pixels': self.options.probe_max_pixels}),)
        if content_kind is not None and 'similarity' in self.router.stages:
            from media_tag.similarity.pipeline import VERSION
            definition = self.router.stages['similarity']
            steps += (StepSpec(key='similarity', stage='similarity', depends_on=('probe',),
                versions=Versions(rules_version=versions.rules_version, preprocess_version=VERSION),
                config={'content_kind': content_kind, 'tool_digest': definition.implementation_version.rsplit(':', 1)[1]}),)
        if content_kind in (None, 'audio'):
            return steps
        if content_kind == 'image' and 'local_topics' in self.router.stages:
            from media_tag.local_topics.contracts import LocalTopicOptions
            from media_tag.local_topics.prompt import PROMPT_VERSION
            from media_tag.local_topics.service import PREPROCESS_VERSION
            definition = self.router.stages['local_topics']
            policy = getattr(self.catalog, 'review_policy', None)
            if policy is None:
                raise ValueError('local topic drafts require published validation policy')
            steps += (StepSpec(key='local_topics', stage='local_topics', depends_on=('probe',),
                config=LocalTopicOptions(model_revision=definition.required_model_revision, policy_digest=policy.digest).model_dump(mode='json'),
                versions=Versions(rules_version='sha256:'+policy.digest, preprocess_version=PREPROCESS_VERSION,
                    taxonomy_version=policy.config.taxonomy_version, model_revision=definition.required_model_revision),
                target_fields=TOPIC_FIELDS, prompt_version=PROMPT_VERSION,
                limits=TaskLimits(gpu_slots=1, timeout_seconds=min(240, self.router.options.max_timeout_seconds))),)
        steps += (StepSpec(key='evidence', stage='evidence', depends_on=('probe',), versions=versions),)
        for kind in ('ocr', 'face', 'clip', 'visual_fields'):
            if kind == 'visual_fields' and content_kind == 'image' and 'local_topics' in self.router.stages:
                continue
            definition = self.router.stages.get(kind)
            if definition and definition.required_model_revision:
                steps += (StepSpec(key=kind, stage=kind, depends_on=('evidence',),
                    versions=Versions(rules_version=versions.rules_version,
                        preprocess_version=definition.required_preprocess_version,
                        model_revision=definition.required_model_revision),
                    target_fields=definition.allowed_target_fields or (),
                    limits=TaskLimits(gpu_slots=definition.required_gpu_slots)),)
        return steps

    def _adopt_probe(self, intent, job_id, fingerprint, principal):
        """Classify only from a current, released, source-bound real probe artifact.

        Adding missing downstream stages preserves the successful probe. If replanning
        commits but its acknowledgement is lost, the next scan verifies the same plan
        and finishes adoption without adding another generation or re-running probe.
        """
        with self.database.transaction() as connection:
            job = self.router._job_auth(connection, job_id, principal)
            if job['cancelled'] or job['paused']:
                return {'intent_id': intent['intent_id'], 'state': 'cancelled' if job['cancelled'] else 'paused'}
            row = connection.execute('''SELECT a.envelope_json,a.result_json FROM router_tasks t
                JOIN router_attempts a USING(task_id) WHERE t.job_id=? AND t.step_key='probe'
                AND t.active=1 AND t.state='succeeded' AND a.lease_epoch=t.lease_epoch
                AND a.released_at IS NOT NULL AND a.result_json IS NOT NULL''', (job_id,)).fetchone()
            if row is None:
                state = self.router._view(connection, job)['state']
                return {'intent_id': intent['intent_id'], 'state': state if state in ('failed', 'needs_attention') else 'awaiting_probe'}
            task = TaskEnvelope.model_validate_json(row['envelope_json'])
            result = WorkerResult.model_validate_json(row['result_json'])
            if (task.stage != 'probe' or task.job_id != job_id or result.status != 'succeeded'
                    or task.asset_id != intent['asset_id'] or task.input_refs[0].sha256 != intent['source_sha256']):
                raise ValueError('probe source changed')
            self.router._artifacts(task, result)
            reports = [ref for ref in result.artifacts if ref.kind == 'technical_report']
            if len(reports) != 1:
                raise ValueError('one verified probe report required')
            with self.router.files.open_read(reports[0].ref) as stream:
                data = stream.read(1024*1024+1)
            if len(data) > 1024*1024 or sha256(data).hexdigest() != reports[0].sha256:
                raise ValueError('probe report integrity changed')
            info = json.loads(data)
            kind = KINDS.get(info.get('kind')) if isinstance(info, dict) else None
            if kind is None:
                raise ValueError('unsupported probe kind')
            expected = {step.key: step for step in self._steps(fingerprint, kind)}
            current = {row['step_key']: StepSpec.model_validate_json(row['spec_json']) for row in
                connection.execute('SELECT step_key,spec_json FROM router_tasks WHERE job_id=? AND active=1', (job_id,))}
            if any(key not in expected or spec != expected[key] for key, spec in current.items()):
                raise ValueError('bound analysis plan changed')
            patches = tuple(spec for key, spec in expected.items() if key not in current)
            revision = job['revision']
        if patches:
            self.router.replan(job_id, patches, expected_revision=revision, principal=principal)
        with self.database.transaction(write=True) as connection:
            asset = self.catalog._authorize(connection, intent['asset_id'], principal, 'edit')
            if self.files._gate(connection, asset):
                return {'intent_id': intent['intent_id'], 'state': 'awaiting_file_gate'}
            ensure_intent(connection, intent['asset_id'], principal.actor_id, content_kind=kind)
        return {'intent_id': intent['intent_id'], 'state': 'probe_adopted', 'job_id': job_id}

    def dispatch_pending(self, *, principal):
        """Dispatch only this authenticated requester, using Router's durable request binding.

        Explicit cancellation, failure, or configuration changes never silently replace a
        bound job. Unknown media kinds first run probe, then add missing visual stages.
        """
        if principal.role not in ('editor', 'admin'):
            raise AccessDenied('human analysis requester required')
        if not self.options.enabled:
            return []
        with self.database.transaction() as connection:
            query = '''SELECT i.*, i.rowid AS scan_position, r.job_id AS bound_job_id
                FROM analysis_intents i LEFT JOIN router_requests r
                ON r.owner_id=i.requested_by AND r.request_id=i.intent_id
                WHERE requested_by=? AND policy_version=?
                AND i.rowid>?
                AND (i.content_kind IS NULL OR r.job_id IS NULL)
                ORDER BY i.rowid LIMIT ?'''
            position = self._dispatch_positions.get(principal.actor_id, 0)
            def scan(after):
                return connection.execute(query, (principal.actor_id, INTAKE_POLICY, after,
                    self.options.dispatch_limit)).fetchall()
            rows = scan(position)
            if not rows and position:
                rows = scan(0)
            fingerprint = connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()[0]
        outcomes = []
        self._dispatch_positions[principal.actor_id] = rows[-1]['scan_position'] if rows else 0
        for intent in rows:
            try:
                with self.database.transaction() as connection:
                    asset = self.catalog._authorize(connection, intent['asset_id'], principal, 'edit')
                    if asset['blob_sha256'] != intent['source_sha256'] or asset['owner_id'] != intent['owner_id']:
                        raise ValueError('analysis intent source changed')
                    if self.files._gate(connection, asset):
                        outcomes.append({'intent_id': intent['intent_id'], 'state': 'awaiting_file_gate'})
                        continue
                if intent['bound_job_id']:
                    outcomes.append(self._adopt_probe(intent, intent['bound_job_id'], fingerprint, principal))
                    continue
                job = self.router.submit(JobSpec(asset_id=intent['asset_id'],
                    steps=self._steps(fingerprint, intent['content_kind'])),
                    request_id=intent['intent_id'], principal=principal)
                outcomes.append({'intent_id': intent['intent_id'], 'state': 'dispatched', 'job_id': job['job_id']})
            except (CatalogError, StorageError, ValueError):
                outcomes.append({'intent_id': intent['intent_id'], 'state': 'dispatch_unavailable'})
        return outcomes

    def status(self, asset_id, *, principal):
        if principal.role not in ("reader", "editor", "admin"):
            raise AccessDenied("human read access required")
        with self.database.transaction() as connection:
            asset = self.catalog._authorize(connection, asset_id, principal)
            row = connection.execute(
                "SELECT * FROM analysis_intents WHERE asset_id=? AND requested_by=? AND policy_version=?",
                (asset_id, principal.actor_id, INTAKE_POLICY),
            ).fetchone()
            gate = self.files._gate(connection, asset)
            if row is None:
                return {"asset_id": asset_id, "state": "not_requested", "analysis_complete": False}
            if row["source_sha256"] != asset["blob_sha256"] or row["owner_id"] != asset["owner_id"]:
                raise ValueError("analysis intent source changed")
            try:
                self.catalog._authorize(connection, asset_id, principal, "edit")
                editable = True
            except AccessDenied:
                editable = False
            state = ("awaiting_file_gate" if gate else "awaiting_edit_permission" if not editable
                     else "awaiting_probe" if row["content_kind"] is None else "pending_analysis")
            runtime, job, topic_analysis = None, None, None
            if not gate and editable:
                binding = connection.execute('SELECT job_id FROM router_requests WHERE owner_id=? AND request_id=?',
                    (principal.actor_id, row['intent_id'])).fetchone()
                if binding:
                    job = self.router._job_auth(connection, binding['job_id'], principal)
                    self.router._source(connection, job)
                    view = self.router._view(connection, job)
                    runtime = {key: view[key] for key in ('job_id', 'revision', 'state', 'tasks')}
                    if view['state'] == 'succeeded':
                        state = ('awaiting_probe_adoption' if row['content_kind'] is None else
                            'awaiting_audio_provider' if row['content_kind'] == 'audio' else 'awaiting_topic_analysis')
                    else:
                        state = view['state']
                if row['content_kind'] is not None:
                    projection = TopicProjection(self.router)
                    document, local_state = projection.build(connection, asset, row['content_kind'], job)
                    topic_analysis = projection.public(document, local_state)
                    topic_analysis['precision_stages'] = projection.precision_states
                    topic_analysis['local_topic_stages'] = projection.local_topic_states
                    topic_analysis['audio_topic_stages'] = projection.audio_topic_states
                    if document.analysis_complete:
                        state = 'analysis_draft_ready'
                    elif projection.precision_states and any(value == 'verified_precision_draft' for value in projection.precision_states.values()) and runtime and runtime['state'] == 'succeeded':
                        state = 'analysis_needs_review'
            return {"asset_id": asset_id, "intent_id": row["intent_id"], "state": state,
                    "content_kind": row["content_kind"], "policy_version": row["policy_version"],
                    "topic_fields": list(TOPIC_FIELDS), "analysis_complete": bool(topic_analysis and topic_analysis['analysis_complete']),
                    "runtime_dispatched": runtime is not None, "runtime": runtime,
                    "topic_analysis": topic_analysis,
                    "dispatch_enabled": self.options.enabled, "medical_confirmation": False,
                    "effective_values_written": False}
