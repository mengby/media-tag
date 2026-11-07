"""Authorized scan/edit/render workflow and explicit, hash-bound human review."""
from hashlib import sha256
import json
from uuid import uuid4

from media_tag.catalog import AccessDenied, NotFound, Principal, RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import _human, encode
from media_tag.contracts.models import Versions, WorkerResult, TaskEnvelope
from media_tag.router.models import JobSpec, StepSpec
from media_tag.storage import IntegrityError
from media_tag.variants.models import PlanSpec, Recipe, MaskSpec
from media_tag.variants.service import VariantService
from .models import TRACK_VERSION, RENDER_VERSION
from .redaction import revise
from .policy import review_state


def digest(value):
    return sha256(encode(value).encode()).hexdigest()


class RedactionService:
    def __init__(self, router, tools, registry, *, reviewers=()):
        self.router,self.database,self.catalog,self.registry=router,router.database,router.catalog,registry
        self.variants=VariantService(router,tools,output_stages=('redaction_render',))
        self.reviewers=frozenset(reviewers)

    def start_scan(self, spec, *, principal):
        _human(principal)
        plan=self.variants.create_plan(PlanSpec(parent_asset_id=spec.parent_asset_id,expected_revision=spec.expected_revision,
            chain_from_derived=spec.chain_from_derived,recipe=Recipe(audio='drop',renderer_revision=TRACK_VERSION,model_revision=self.registry.revision)),
            request_id='scan-plan-'+digest({'actor':principal.actor_id,'request':spec.request_id}),principal=principal)
        config=spec.options.model_dump()|{'variant_plan_id':plan['plan_id'],'variant_plan_hash':plan['recipe_hash']}
        versions=Versions(rules_version=TRACK_VERSION,preprocess_version=TRACK_VERSION,model_revision=self.registry.revision)
        return self.router.submit(JobSpec(asset_id=spec.parent_asset_id,priority=1,steps=(StepSpec(key='face-track',stage='face_track',config=config,versions=versions),)),
            request_id='scan-'+digest({'request_id':spec.request_id}),principal=principal)

    def get_scan(self, job_id, *, principal):
        status=self.router.get_job(job_id,principal=principal)
        tasks=[t for t in status['tasks'] if t['stage']=='face_track']
        if len(tasks)!=1: raise NotFound('not a face scan job')
        result={'job':status,'data':None}
        if status['state']!='succeeded': return result
        task=tasks[0]
        with self.database.transaction() as connection:
            row=connection.execute('SELECT result_json FROM router_attempts WHERE task_id=? AND lease_epoch=?',(task['task_id'],task['lease_epoch'])).fetchone()
            outcome=WorkerResult.model_validate_json(row[0])
            refs=[r for r in outcome.artifacts if r.kind=='face_tracks']
            if len(refs)!=1: raise IntegrityError('one full scan report required')
            from .models import RevisionSubmission
            spec=RevisionSubmission(scan_task_id=task['task_id'],lease_epoch=task['lease_epoch'],report_sha256=refs[0].sha256)
            _,report=self._scan(connection,spec,principal)
        return result|spec.model_dump()|{'data':report}

    def _scan(self, connection, spec, principal):
        _human(principal)
        row=connection.execute('''SELECT a.*,t.active,t.state,t.lease_epoch AS current_epoch,j.owner_id,j.cancelled
           FROM router_attempts a JOIN router_tasks t USING(task_id) JOIN router_jobs j USING(job_id)
           WHERE a.task_id=? AND a.lease_epoch=?''',(spec.scan_task_id,spec.lease_epoch)).fetchone()
        if row is None: raise NotFound('scan unavailable')
        if row['owner_id']!=principal.actor_id: raise AccessDenied('scan owner required')
        if row['cancelled'] or not row['active'] or row['state']!='succeeded' or row['current_epoch']!=spec.lease_epoch or row['released_at'] is None:
            raise RevisionConflict('scan is no longer current and stopped')
        task=TaskEnvelope.model_validate_json(row['envelope_json']); result=WorkerResult.model_validate_json(row['result_json'])
        if task.stage!='face_track' or task.versions.preprocess_version!=TRACK_VERSION or task.versions.model_revision!=self.registry.revision or result.status!='succeeded':
            raise RevisionConflict('registered full-frame scan required')
        self.catalog._authorize(connection,task.asset_id,principal)
        self.router._artifacts(task,result)
        refs=[a for a in result.artifacts if a.kind=='face_tracks' and a.sha256==spec.report_sha256]
        if len(refs)!=1: raise IntegrityError('exact accepted scan report required')
        with self.router.files.open_read(refs[0].ref) as stream:
            raw=stream.read(32*1024**2+1)
        if len(raw)>32*1024**2 or sha256(raw).hexdigest()!=spec.report_sha256: raise IntegrityError('scan changed')
        report=json.loads(raw)
        if report['source_sha256']!=task.input_refs[0].sha256 or report['model_revision']!=task.versions.model_revision or not report['all_decoded_frames_observed'] or report['frame_count']!=len(report['frames']):
            raise IntegrityError('scan source or coverage mismatch')
        return task,report

    def create_revision(self, spec, *, principal):
        with self.database.transaction() as connection:
            task,report=self._scan(connection,spec,principal)
        payload=revise(report,spec.edits)
        payload.update(scan_task_id=spec.scan_task_id,lease_epoch=spec.lease_epoch,report_sha256=spec.report_sha256,
                       model_revision=self.registry.revision,actor_id=principal.actor_id)
        revision_hash=digest(payload)
        with self.database.transaction(write=True) as connection:
            self._scan(connection,spec,principal)
            connection.execute('INSERT OR IGNORE INTO redaction_revisions VALUES(?,?,?,?,?,?,?,?,?)',
                (revision_hash,principal.actor_id,task.asset_id,task.input_refs[0].sha256,spec.scan_task_id,spec.lease_epoch,spec.report_sha256,encode(payload),utc_now()))
        return {'revision_hash':revision_hash,'source_sha256':payload['source_sha256'],'frame_count':len(payload['frames']),
                'edits':payload['edits'],'excluded_tracks':payload['excluded_tracks'],'privacy_confirmation':False}

    def revision(self, revision_hash, *, principal):
        _human(principal)
        with self.database.transaction() as connection:
            row=connection.execute('SELECT * FROM redaction_revisions WHERE revision_hash=?',(revision_hash,)).fetchone()
            if row is None: raise NotFound('revision unavailable')
            if row['owner_id']!=principal.actor_id: raise AccessDenied('revision owner required')
            self.catalog._authorize(connection,row['asset_id'],principal)
            from .models import RevisionSubmission
            self._scan(connection,RevisionSubmission(scan_task_id=row['scan_task_id'],lease_epoch=row['lease_epoch'],report_sha256=row['report_sha256']),principal)
            payload=json.loads(row['payload_json'])
            if digest(payload)!=revision_hash: raise IntegrityError('revision integrity failed')
            return dict(row),payload

    def start_render(self, spec, *, principal):
        row,payload=self.revision(spec.revision_hash,principal=principal)
        fingerprint=digest(spec.model_dump())
        with self.database.transaction() as connection:
            old=connection.execute('SELECT * FROM redaction_runs WHERE owner_id=? AND request_id=?',(principal.actor_id,spec.request_id)).fetchone()
            if old:
                if old['fingerprint']!=fingerprint: raise RevisionConflict('render request reused with changed recipe')
                return dict(old)
        asset=self.catalog.get_asset(row['asset_id'],principal=principal)
        recipe=Recipe(speed=spec.speed,segments=spec.segments,mask=MaskSpec(revision=spec.revision_hash,mode=spec.mode,strength=spec.strength,padding=spec.padding),
            audio=spec.audio,audio_stream_index=spec.audio_stream_index,renderer_revision=RENDER_VERSION,model_revision=self.registry.revision)
        plan=self.variants.create_plan(PlanSpec(parent_asset_id=asset.asset_id,expected_revision=asset.revision,recipe=recipe,chain_from_derived=spec.chain_from_derived),
            request_id='redact-plan-'+digest({'owner':principal.actor_id,'request':spec.request_id}),principal=principal)
        config={'variant_plan_id':plan['plan_id'],'variant_plan_hash':plan['recipe_hash'],'revision_hash':spec.revision_hash}
        versions=Versions(rules_version=RENDER_VERSION,preprocess_version=RENDER_VERSION,model_revision=self.registry.revision)
        job=self.router.submit(JobSpec(asset_id=asset.asset_id,priority=1,steps=(StepSpec(key='render',stage='redaction_render',config=config,versions=versions),)),
            request_id='redact-'+digest({'request_id':spec.request_id}),principal=principal)
        with self.database.transaction(write=True) as connection:
            connection.execute('INSERT OR IGNORE INTO redaction_runs VALUES(?,?,?,?,?,?,?,NULL)',
                ('redact-'+uuid4().hex,principal.actor_id,spec.request_id,fingerprint,spec.revision_hash,plan['plan_id'],job['job_id']))
            row=connection.execute('SELECT * FROM redaction_runs WHERE owner_id=? AND request_id=?',(principal.actor_id,spec.request_id)).fetchone()
            if row['fingerprint']!=fingerprint: raise RevisionConflict('concurrent render request conflict')
            return dict(row)

    def adopt_ready(self):
        # Internal bootstrap capability, never exposed as a worker/admin request body.
        admin=Principal(actor_id='local-redaction-adoption',role='admin')
        with self.database.transaction() as connection:
            pending=connection.execute('SELECT * FROM redaction_runs WHERE asset_id IS NULL').fetchall()
        for row in pending:
            with self.database.transaction() as connection:
                attempt=connection.execute('''SELECT a.* FROM router_tasks t JOIN router_attempts a USING(task_id)
                   JOIN router_jobs j USING(job_id) WHERE t.job_id=? AND t.active=1 AND t.state='succeeded' AND j.cancelled=0
                   AND t.lease_epoch=a.lease_epoch AND a.released_at IS NOT NULL''',(row['job_id'],)).fetchone()
            if attempt is None: continue
            result=WorkerResult.model_validate_json(attempt['result_json'])
            outputs=[a for a in result.artifacts if a.kind=='redacted_video']
            if len(outputs)!=1: raise IntegrityError('one accepted redacted output required')
            asset=self.variants.register_output(row['plan_id'],outputs[0],task_id=attempt['task_id'],lease_epoch=attempt['lease_epoch'],principal=admin)
            with self.database.transaction(write=True) as connection:
                connection.execute('UPDATE redaction_runs SET asset_id=? WHERE run_id=? AND asset_id IS NULL',(asset.asset_id,row['run_id']))

    def get_run(self, run_id, *, principal):
        _human(principal)
        with self.database.transaction() as connection:
            row=connection.execute('SELECT * FROM redaction_runs WHERE run_id=?',(run_id,)).fetchone()
            if row is None: raise NotFound('render unavailable')
            if row['owner_id']!=principal.actor_id: raise AccessDenied('render owner required')
            variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(row['asset_id'],)).fetchone() if row['asset_id'] else None
            privacy=review_state(connection,variant) if variant else 'output_unavailable'
        status=self.router.get_job(row['job_id'],principal=principal)
        return dict(row)|{'job':status,'privacy_review':privacy}

    def _review_access(self, connection, asset, principal):
        _human(principal)
        self.catalog._authorize(connection,asset['asset_id'],principal)
        run=connection.execute('SELECT * FROM redaction_runs WHERE asset_id=?',(asset['asset_id'],)).fetchone()
        if run is None: raise AccessDenied('not a registered redaction output')
        if asset['owner_id']!=principal.actor_id and principal.actor_id not in self.reviewers:
            raise AccessDenied('private output owner or assigned reviewer required')

    def review_files(self):
        from media_tag.files.service import FileService
        return FileService(self.router,review_authorizer=self._review_access)

    def review(self, asset_id, spec, *, principal):
        _human(principal)
        if principal.actor_id not in self.reviewers: raise AccessDenied('explicit privacy reviewer binding required')
        if spec.decision=='approved' and not (spec.full_output_viewed and spec.exclusions_checked and spec.risk_intervals_checked):
            raise ValueError('full output, exclusions and risk intervals must be explicitly checked')
        # Approval requires actual bytes and a current producer. Revocation only reduces access,
        # so a missing/corrupt file or cancelled producer must not prevent it.
        if spec.decision=='approved':
            opened=self.review_files().open(asset_id,spec.output_sha256,principal=principal)
            opened.close()
        with self.database.transaction(write=True) as connection:
            asset=self.catalog._authorize(connection,asset_id,principal)
            self._review_access(connection,asset,principal)
            revision=self.catalog._expected(asset,spec.expected_revision)
            variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(asset_id,)).fetchone()
            if variant['recipe_hash']!=spec.recipe_hash or variant['output_sha256']!=spec.output_sha256:
                raise RevisionConflict('review must bind exact output and recipe')
            if spec.decision=='approved' and self.review_files()._gate(connection,asset,review_asset_id=asset_id):
                raise RevisionConflict('producer or lineage no longer available')
            connection.execute('INSERT INTO privacy_reviews(asset_id,output_sha256,recipe_hash,decision,actor_id,asset_revision,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (asset_id,spec.output_sha256,spec.recipe_hash,spec.decision,principal.actor_id,revision,encode(spec.model_dump()),utc_now()))
            connection.execute('UPDATE assets SET revision=? WHERE asset_id=?',(revision,asset_id))
            self.catalog._event(connection,asset_id,revision,principal,'privacy_review',{},spec.model_dump())
            return {'asset_id':asset_id,'revision':revision,'decision':spec.decision,'output_sha256':spec.output_sha256,
                    'medical_confirmation':False,'license_confirmation':False}
