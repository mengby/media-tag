"""Catalogue-only processing requests and gated, hash-verified status metadata."""
import json

from media_tag.catalog import AccessDenied, NotFound, Principal, RevisionConflict
from media_tag.catalog.service import encode
from media_tag.contracts.models import TaskLimits, Versions
from media_tag.files.service import FileService
from media_tag.router.models import JobSpec, StepSpec
from media_tag.storage import StorageError

from .models import CleaningSubmission, CleaningTask, ImageSubmission, ImageTask, ProcessingDeployment, STAGES


def source_gate(router, connection, asset_id, principal, source_sha256, expected_revision=None):
    if principal.role not in ('editor', 'admin'):
        raise AccessDenied('human processing editor required')
    # Queue jobs persist an actor ID, not an externally authenticated admin role.
    # Apply the same current object ACL both before queueing and in the worker.
    asset = router.catalog._authorize(connection, asset_id, Principal(actor_id=principal.actor_id), 'edit')
    if asset['blob_sha256'] != source_sha256 or expected_revision is not None and asset['revision'] != expected_revision:
        raise RevisionConflict('processing source changed')
    if FileService(router)._gate(connection, asset):
        raise AccessDenied('processing requires a currently available authorized source')
    return asset


class ProcessingService:
    def __init__(self, router, tools, options=None):
        self.router, self.catalog, self.database = router, router.catalog, router.database
        self.tools, self.options = tools, options or ProcessingDeployment()

    def request(self, spec, *, principal):
        from .runtime import implementation
        if not isinstance(spec, (CleaningSubmission, ImageSubmission)):
            raise ValueError('strict catalogue processing submission required')
        if not self.options.enabled:
            raise RevisionConflict('processing dispatch is disabled')
        with self.database.transaction() as connection:
            asset = source_gate(self.router, connection, spec.asset_id, principal, spec.source_sha256, spec.expected_revision)
            binding = dict(expected_revision=spec.expected_revision, source_sha256=spec.source_sha256,
                requester_id=principal.actor_id, source_owner_id=asset['owner_id'])
        if isinstance(spec, CleaningSubmission):
            stage = 'cleaning_output'
            config = CleaningTask(**binding, recipe=spec.recipe, limits=self.options.cleaning)
        else:
            stage = 'image_mask_output'
            config = ImageTask(**binding, recipe=spec.recipe, limits=self.options.image)
        step = StepSpec(key='processing', stage=stage, config=config.model_dump(mode='json'),
            versions=Versions(rules_version='sha256:'+self.catalog.contract.fingerprint,
                preprocess_version=implementation(self.tools)),
            limits=TaskLimits(timeout_seconds=min(self.options.timeout_seconds, self.router.options.max_timeout_seconds),
                max_attempts=min(3, self.router.options.max_attempts), gpu_slots=0))
        view = self.router.submit(JobSpec(asset_id=spec.asset_id, steps=(step,)), request_id=spec.request_id, principal=principal)
        # Router.submit owns its own transaction. A race here never returns success:
        # cancel the new/current job, and the handler independently enforces the pin.
        try:
            with self.database.transaction() as connection:
                current = source_gate(self.router, connection, spec.asset_id, principal, spec.source_sha256, spec.expected_revision)
                if current['owner_id'] != config.source_owner_id:
                    raise RevisionConflict('processing source owner changed')
        except (AccessDenied, NotFound, RevisionConflict):
            self.router.cancel(view['job_id'], principal=principal)
            raise
        return {'job_id': view['job_id'], 'asset_id': spec.asset_id, 'state': view['state'],
            'request_accepted': True, 'processing_completed': False, 'asset_registered': False,
            'output_download_available': False, 'privacy_confirmation': False, 'quality_confirmation': False}

    def status(self, job_id, *, principal):
        from .verification import require_current_attempt, verified_result
        job,row,asset,config,base = self._status_snapshot(job_id,principal)
        if base['state'] in ('source_changed','cancelled') or row['released_at'] is None or not row['result_json']:
            return base
        try:
            task,result,report,ref = verified_result(self.router,self.tools,row,asset)
            if report is None:
                answer = base | {'state':'failed','issue_code':'processing_execution_failed'}
            else:
                public_summary = {key:value for key,value in report.summary.items() if key in
                    ('content_kind','frame_count','animation_frame_count','display_width','display_height',
                     'duration_target_state','output_duration_seconds','speed','mask_region_count',
                     'watermark_quality_status','full_output_decode','cache_reused')}
                answer = base | {'state':report.state,'processing_completed':report.state=='pending_registration_review',
                    'registration_status':report.registration_status,'review_status':report.review_status,
                    'output_sha256':report.output.sha256 if report.output else None,'output_bytes':report.output_bytes,
                    'recipe_sha256':report.recipe_sha256,'renderer_version':report.renderer_version,
                    'issue_code':report.issue_code,'summary':public_summary}
        except (StorageError,OSError,ValueError,KeyError,TypeError):
            answer = base | {'state':'processing_evidence_unavailable'}
        # No read or writer transaction spans the long media hash pass. This
        # fresh authorization/lease/result snapshot is the final disclosure gate.
        current_job,current_row,current_asset,current_config,current_base = self._status_snapshot(job_id,principal)
        if current_base['state'] in ('source_changed','cancelled'):
            return current_base
        with self.database.transaction() as connection:
            self.router._job_auth(connection,job_id,principal)
            latest = source_gate(self.router,connection,current_job['asset_id'],principal,config.source_sha256)
            if latest['revision'] != config.expected_revision or latest['owner_id'] != config.source_owner_id:
                return current_base | {'state':'source_changed'}
            try:
                require_current_attempt(connection,row)
            except ValueError:
                return current_base | {'state':'processing_evidence_unavailable'}
            if answer['processing_completed']:
                registered=connection.execute('''SELECT a.*,v.output_sha256 FROM variants v JOIN assets a USING(asset_id)
                    JOIN processing_derivations p USING(asset_id) WHERE v.task_id=? AND v.lease_epoch=?''',
                    (row['task_id'],row['lease_epoch'])).fetchone()
                if registered and registered['output_sha256']==answer['output_sha256']:
                    self.catalog._authorize(connection,registered['asset_id'],principal)
                    blocked=FileService(self.router)._gate(connection,registered)
                    answer |= {'asset_registered':True,'derived_asset_id':registered['asset_id'],'registration_status':'registered',
                        'state':'registered_pending_review' if blocked else 'registered_available','output_download_available':blocked is None}
                else:
                    receipt=connection.execute('SELECT * FROM processing_adoption_receipts WHERE task_id=? AND lease_epoch=?',(row['task_id'],row['lease_epoch'])).fetchone()
                    if receipt and receipt['state']=='failed':
                        answer['registration_attempt']={'state':'adoption_not_completed','code':receipt['error_code'],'retry_after':receipt['retry_after']}
        return answer

    def _status_snapshot(self, job_id, principal):
        with self.database.transaction() as connection:
            job = self.router._job_auth(connection, job_id, principal)
            rows = connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at
                FROM router_tasks t LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                WHERE t.job_id=? AND t.active=1 ORDER BY t.rowid DESC''', (job_id,)).fetchall()
            rows = [row for row in rows if json.loads(row['spec_json'])['stage'] in STAGES]
            if len(rows) != 1:
                raise NotFound('processing job unavailable')
            row = rows[0]
            config_raw = json.loads(row['spec_json'])['config']
            config_type = CleaningTask if config_raw['operation'] == 'cleaning' else ImageTask
            config = config_type.model_validate_json(encode(config_raw))
            asset = source_gate(self.router, connection, job['asset_id'], principal, config.source_sha256)
            view = self.router._view(connection, job)
            base = {'job_id': job_id, 'asset_id': job['asset_id'], 'runtime_state': view['state'],
                'state': 'cancelled' if job['cancelled'] else row['state'], 'processing_completed': False,
                'asset_registered': False, 'output_download_available': False,
                'privacy_confirmation': False, 'quality_confirmation': False, 'medical_confirmation': False,
                'effective_values_written': False}
            if asset['revision'] != config.expected_revision or asset['owner_id'] != config.source_owner_id:
                base |= {'state':'source_changed'}
            return job,row,asset,config,base
