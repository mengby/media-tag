"""Read authorized, requester-scoped intake state without declaring analysis complete."""

from media_tag.catalog import AccessDenied, CatalogError
from media_tag.contracts.models import TaskLimits, Versions
from media_tag.files.service import FileService
from media_tag.router.models import JobSpec, StepSpec

from .contracts import TOPIC_FIELDS
from .intake import INTAKE_POLICY
from .deployment import AnalysisDeployment


class AnalysisService:
    def __init__(self, router, options=None):
        self.router, self.catalog, self.database = router, router.catalog, router.database
        self.files = FileService(router)
        self.options = options or AnalysisDeployment()
        self._dispatch_positions = {}

    def _steps(self, fingerprint, content_kind):
        versions = Versions(rules_version=f'sha256:{fingerprint}', preprocess_version='d02-cpu-v1')
        steps = (StepSpec(key='probe', stage='probe', versions=versions,
                          config={'max_pixels': self.options.probe_max_pixels}),)
        if content_kind == 'audio':
            return steps
        steps += (StepSpec(key='evidence', stage='evidence', depends_on=('probe',), versions=versions),)
        for kind in ('ocr', 'face', 'clip', 'visual_fields'):
            definition = self.router.stages.get(kind)
            if definition and definition.required_model_revision:
                steps += (StepSpec(key=kind, stage=kind, depends_on=('evidence',),
                    versions=Versions(rules_version=versions.rules_version,
                        preprocess_version=definition.required_preprocess_version,
                        model_revision=definition.required_model_revision),
                    target_fields=definition.allowed_target_fields or (),
                    limits=TaskLimits(gpu_slots=definition.required_gpu_slots)),)
        return steps

    def dispatch_pending(self, *, principal):
        """Dispatch only this authenticated requester, using Router's durable request binding.

        Explicit cancellation, failure, or configuration changes never silently replace a
        bound job. Unknown media kinds still require a verified probe before this pipeline.
        """
        if principal.role not in ('editor', 'admin'):
            raise AccessDenied('human analysis requester required')
        if not self.options.enabled:
            return []
        with self.database.transaction() as connection:
            rows = connection.execute('''SELECT i.*, i.rowid AS scan_position FROM analysis_intents i
                WHERE requested_by=? AND policy_version=? AND content_kind IS NOT NULL
                AND i.rowid>?
                AND NOT EXISTS (SELECT 1 FROM router_requests r
                    WHERE r.owner_id=i.requested_by AND r.request_id=i.intent_id)
                ORDER BY i.rowid LIMIT ?''',
                (principal.actor_id, INTAKE_POLICY, self._dispatch_positions.get(principal.actor_id, 0),
                 self.options.dispatch_limit)).fetchall()
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
                job = self.router.submit(JobSpec(asset_id=intent['asset_id'],
                    steps=self._steps(fingerprint, intent['content_kind'])),
                    request_id=intent['intent_id'], principal=principal)
                outcomes.append({'intent_id': intent['intent_id'], 'state': 'dispatched', 'job_id': job['job_id']})
            except (CatalogError, ValueError):
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
            runtime = None
            if not gate and editable:
                binding = connection.execute('SELECT job_id FROM router_requests WHERE owner_id=? AND request_id=?',
                    (principal.actor_id, row['intent_id'])).fetchone()
                if binding:
                    job = self.router._job_auth(connection, binding['job_id'], principal)
                    self.router._source(connection, job)
                    view = self.router._view(connection, job)
                    runtime = {key: view[key] for key in ('job_id', 'revision', 'state', 'tasks')}
                    state = ('awaiting_audio_provider' if row['content_kind'] == 'audio' else 'awaiting_topic_analysis') if view['state'] == 'succeeded' else view['state']
            return {"asset_id": asset_id, "intent_id": row["intent_id"], "state": state,
                    "content_kind": row["content_kind"], "policy_version": row["policy_version"],
                    "topic_fields": list(TOPIC_FIELDS), "analysis_complete": False,
                    "runtime_dispatched": runtime is not None, "runtime": runtime,
                    "dispatch_enabled": self.options.enabled, "medical_confirmation": False,
                    "effective_values_written": False}
