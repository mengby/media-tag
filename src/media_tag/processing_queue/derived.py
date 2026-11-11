"""Versioned processing derivations, atomic publication and independent review."""
from hashlib import sha256
import json
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from media_tag.catalog import AccessDenied,NotFound,Principal,RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import encode
from media_tag.files.service import FileService
from media_tag.storage import IntegrityError,StorageError
from media_tag.workers.probe import MediaError
from media_tag.transforms.policy import processing_contract,processing_review_states,review_state

from .derived_contracts import CONTRACT_VERSION,FaceProcessingProof,OutputReview,ProcessingTimeMap,ProcessingVariantContract,ReviewRequirements
from .derived_validation import validate_output
from .models import ProcessingDeployment
from .service import ProcessingService,source_gate
from .verification import require_current_attempt,verified_result


class ProcessingDerivedService:
    def __init__(self,router,tools,*,options=None):
        self.router,self.catalog,self.database,self.tools=router,router.catalog,router.database,tools
        self.options=options or ProcessingDeployment()
        self.processing=ProcessingService(router,tools,options=self.options)

    def _enabled(self):
        if not self.options.enabled:raise RevisionConflict('processing is disabled')

    def adopt(self,job_id,*,principal):
        self._enabled()
        started=time.monotonic()
        try:
            value=self._adopt(job_id,principal=principal)
        except Exception as exc:
            code=('adoption_access_denied' if isinstance(exc,AccessDenied) else 'adoption_conflict' if isinstance(exc,(RevisionConflict,NotFound))
                else 'adoption_storage_failed' if isinstance(exc,(StorageError,OSError)) else 'adoption_validation_failed')
            self._receipt(job_id,principal,None,code,time.monotonic()-started)
            raise
        self._receipt(job_id,principal,value['asset_id'],None,time.monotonic()-started)
        return value

    def _receipt(self,job_id,principal,asset_id,code,elapsed):
        try:
            with self.database.transaction(write=True) as connection:
                job=self.router._job_auth(connection,job_id,principal)
                if job['owner_id']!=principal.actor_id:return
                row=connection.execute('''SELECT t.task_id,t.lease_epoch,a.result_digest,s.revision FROM router_tasks t
                    JOIN router_jobs j USING(job_id) JOIN assets s ON s.asset_id=j.asset_id
                    JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                    WHERE t.job_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage') IN ('cleaning_output','image_mask_output')''',(job_id,)).fetchone()
                if row is None or not row['result_digest']:return
                previous=connection.execute('SELECT attempts FROM processing_adoption_receipts WHERE task_id=? AND lease_epoch=?',(row['task_id'],row['lease_epoch'])).fetchone()
                attempts=min((previous[0] if previous else 0)+1,10);now=self.router.clock()
                connection.execute('''INSERT INTO processing_adoption_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(task_id,lease_epoch) DO UPDATE SET result_digest=excluded.result_digest,source_revision=excluded.source_revision,
                    state=excluded.state,asset_id=excluded.asset_id,error_code=excluded.error_code,attempts=excluded.attempts,
                    last_attempt=excluded.last_attempt,retry_after=excluded.retry_after,elapsed_seconds=excluded.elapsed_seconds''',
                    (row['task_id'],row['lease_epoch'],row['result_digest'],row['revision'],'registered' if asset_id else 'failed',asset_id,code,attempts,now,
                     now if asset_id else now+min(300,2**attempts),elapsed))
        except (sqlite3.Error,AccessDenied,NotFound):
            # Registration truth is the atomic variants row, never this retry ledger.
            return

    def _adopt(self,job_id,*,principal):
        self._enabled()
        job,row,asset,config,base=self.processing._status_snapshot(job_id,principal)
        if principal.actor_id!=config.requester_id:
            raise AccessDenied('processing adoption requires the actual requesting actor')
        if base['state'] in ('cancelled','source_changed') or row['state']!='succeeded' or row['released_at'] is None:
            raise RevisionConflict('current successful released processing output required')
        with self.database.transaction() as connection:
            old=connection.execute('SELECT asset_id FROM variants WHERE task_id=? AND lease_epoch=?',(row['task_id'],row['lease_epoch'])).fetchone()
            if old:return self.status(old[0],principal=principal)
        task,result,report,ref=verified_result(self.router,self.tools,row,asset)
        if report is None or report.state!='pending_registration_review':raise RevisionConflict('no produced processing output')
        with self.router.files.open_verified(report.output.ref,report.output.sha256,report.output_bytes) as stream:
            validation=validate_output(Path(stream.name),self.tools,config,report)
            stream.seek(0)
            blob=self.catalog.object_store.ingest(stream)
        if blob.sha256!=report.output.sha256 or blob.size_bytes!=report.output_bytes:
            raise IntegrityError('processing output changed during immutable ingest')
        with self.router.files.open_read(report.core_report.ref) as stream:
            raw=stream.read(16*1024**2+1)
        if len(raw)>16*1024**2 or sha256(raw).hexdigest()!=report.core_report.sha256:
            raise IntegrityError('processing core changed during adoption')
        core=json.loads(raw)
        if config.operation=='image_mask':
            if ([frame['rgba_sha256'] for frame in core['frames']]!=validation['frame_rgba_sha256']
                    or core['frame_count']!=validation['frame_count']):
                raise ValueError('actual decoded frames differ from applied mask report')
            face_frames=tuple(frame['frame_index'] for frame in core['frames'] if any(region['purpose']=='face' for region in frame['regions']))
            face_count=sum(region['purpose']=='face' for frame in core['frames'] for region in frame['regions'])
        else:face_frames,face_count=(),0
        face_proof=FaceProcessingProof(core_report_sha256=report.core_report.sha256,applied=bool(face_count),
            face_region_count=face_count,face_frame_indices=face_frames,frame_count=validation.get('frame_count',0))
        if validation['kind']=='video':
            mapping=core.get('time_map')
            if not mapping:raise ValueError('actual video source/output mapping required')
            clock=ProcessingTimeMap(state='available',mapping=mapping)
            cumulative=float(mapping['speed'])
            with self.database.transaction() as connection:
                prior=connection.execute('SELECT p.spec_json FROM variants v JOIN variant_plans p USING(plan_id) WHERE v.asset_id=?',(asset['asset_id'],)).fetchone()
            if prior:
                previous=json.loads(prior[0]).get('cumulative_speed')
                if previous is None:raise ValueError('parent cumulative video clock unavailable')
                cumulative*=previous
            streams=validation['raw_metadata']['streams']
            video=next(s for s in streams if s.get('codec_type')=='video' and not s.get('disposition',{}).get('attached_pic'))
            if abs(float(video['duration'])-float(mapping['output_duration_seconds']))>.15:
                raise ValueError('actual registered video duration differs from core map')
        else:
            reasons={'image':'image_has_no_video_clock','animation':'animation_timing_preserved_without_video_retime','audio':'audio_not_retimed'}
            clock=ProcessingTimeMap(state='not_applicable',reason=reasons[validation['kind']]);cumulative=None
        watermark=config.operation=='image_mask' and any(region.purpose=='watermark' for region in config.recipe.regions)
        contract=ProcessingVariantContract(operation=config.operation,parent_asset_id=asset['asset_id'],source_sha256=config.source_sha256,
            source_revision=config.expected_revision,source_owner_id=config.source_owner_id,requester_id=config.requester_id,
            recipe=config.recipe.model_dump(mode='json'),recipe_sha256=report.recipe_sha256,renderer_version=report.renderer_version,
            implementation_version=task.versions.preprocess_version,producer_task_id=task.task_id,producer_lease_epoch=task.lease_epoch,
            producer_input_fingerprint=task.input_fingerprint,producer_result_sha256=sha256(row['result_json'].encode()).hexdigest(),
            output_sha256=blob.sha256,output_bytes=blob.size_bytes,content_kind=validation['kind'],time_map=clock,cumulative_speed=cumulative,
            review_requirements=ReviewRequirements(watermark=watermark,privacy=validation['kind']!='audio'),face_processing=face_proof)
        encoded=encode(contract.model_dump(mode='json'));recipe_hash=sha256(encoded.encode()).hexdigest()
        # All hash/decode/ingest work above is outside this short publication transaction.
        with self.database.transaction(write=True) as connection:
            current_job=self.router._job_auth(connection,job_id,principal)
            parent=source_gate(self.router,connection,asset['asset_id'],principal,config.source_sha256,config.expected_revision)
            if (parent['owner_id']!=config.source_owner_id or current_job['owner_id']!=config.requester_id
                    or principal.actor_id!=config.requester_id):
                raise RevisionConflict('processing source ownership changed')
            require_current_attempt(connection,row)
            old=connection.execute('SELECT asset_id FROM variants WHERE task_id=? AND lease_epoch=?',(task.task_id,task.lease_epoch)).fetchone()
            if old:asset_id=old[0]
            else:
                now,plan_id,asset_id=utc_now(),'plan-'+uuid4().hex,'asset-'+uuid4().hex
                connection.execute('INSERT INTO variant_plans VALUES(?,?,?,?,?,?,?)',(plan_id,config.requester_id,parent['asset_id'],config.source_sha256,recipe_hash,encoded,now))
                connection.execute('INSERT OR IGNORE INTO blobs VALUES(?,?,?)',(blob.sha256,blob.size_bytes,now))
                connection.execute('INSERT OR IGNORE INTO storage_locations VALUES(?,?,?)',(blob.sha256,self.catalog.backend_id,blob.object_key))
                if connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?',(blob.sha256,)).fetchone()[0]!=blob.size_bytes:
                    raise IntegrityError('processing blob size conflict')
                if connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=? AND backend_id=?',(blob.sha256,self.catalog.backend_id)).fetchone()[0]!=blob.object_key:
                    raise IntegrityError('processing object location conflict')
                connection.execute("INSERT INTO assets(asset_id,blob_sha256,owner_id,name,revision,kind,root_asset_id,parent_asset_id,created_at) VALUES(?,?,?,?,1,'derived',?,?,?)",
                    (asset_id,blob.sha256,config.requester_id,Path(core['output_ref']).name,parent['root_asset_id'],parent['asset_id'],now))
                connection.execute('INSERT INTO variants VALUES(?,?,?,?,?,?,?,?,?,?,?)',(asset_id,plan_id,config.source_sha256,blob.sha256,recipe_hash,
                    encode(clock.model_dump(mode='json')),encode(validation),'pending' if contract.review_requirements.privacy else 'not_applicable',task.task_id,task.lease_epoch,now))
                connection.execute('INSERT INTO processing_derivations VALUES(?,?,?,?,?,?)',(asset_id,CONTRACT_VERSION,1,int(watermark),recipe_hash,now))
                source_ids={item[0] for item in connection.execute('SELECT source_id FROM asset_sources WHERE asset_id=? UNION SELECT source_id FROM variant_source_links WHERE asset_id=?',(parent['asset_id'],parent['asset_id']))}
                for source_id in source_ids:connection.execute('INSERT INTO variant_source_links VALUES(?,?)',(asset_id,source_id))
                self.catalog._event(connection,asset_id,1,principal,'register_processing_variant',{},
                    {'plan_id':plan_id,'contract_version':CONTRACT_VERSION,'output_sha256':blob.sha256,'tag_origin':'not_inherited','license_confirmation':False})
                from media_tag.analysis.intake import ensure_intent
                ensure_intent(connection,asset_id,config.requester_id,content_kind=validation['kind'],probe_info=validation)
        return self.status(asset_id,principal=principal)

    def adopt_ready(self,*,principals=(),limit=8):
        """Only authenticated server credential actors; no invented admin/owner delegation."""
        if not self.options.enabled or not self.options.auto_adopt:return []
        actors={actor.actor_id:actor for actor in principals if actor.role in ('editor','admin')}
        if not actors:return []
        placeholders=','.join('?' for _ in actors)
        with self.database.transaction() as connection:
            rows=connection.execute('''SELECT DISTINCT t.job_id,j.owner_id FROM router_tasks t JOIN router_jobs j USING(job_id)
                JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                JOIN assets s ON s.asset_id=j.asset_id
                LEFT JOIN variants v ON v.task_id=t.task_id AND v.lease_epoch=t.lease_epoch
                LEFT JOIN processing_adoption_receipts r ON r.task_id=t.task_id AND r.lease_epoch=t.lease_epoch
                WHERE t.active=1 AND t.state='succeeded' AND j.cancelled=0 AND a.released_at IS NOT NULL
                AND json_extract(t.spec_json,'$.stage') IN ('cleaning_output','image_mask_output') AND v.asset_id IS NULL
                AND j.owner_id IN ('''+placeholders+''')
                AND (r.task_id IS NULL OR r.result_digest!=a.result_digest OR r.source_revision!=s.revision OR r.retry_after<=?)
                ORDER BY COALESCE(r.last_attempt,0),t.rowid LIMIT ?''',(*actors,self.router.clock(),min(max(limit,1),32))).fetchall()
        outcomes=[]
        for row in rows:
            try:
                value=self.adopt(row['job_id'],principal=actors[row['owner_id']])
                outcomes.append({'job_id':row['job_id'],'asset_id':value['asset_id'],'state':'registered_pending_review'})
            except (AccessDenied,RevisionConflict,NotFound,ValueError,OSError,StorageError,MediaError):
                outcomes.append({'job_id':row['job_id'],'state':'adoption_not_completed'})
        return outcomes

    def _record(self,connection,asset_id,principal,*,permission='read'):
        self._enabled()
        if principal.role not in ('editor','admin'):raise AccessDenied('human processing actor required')
        actor=Principal(actor_id=principal.actor_id)
        asset=self.catalog._authorize(connection,asset_id,actor,permission)
        variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(asset_id,)).fetchone()
        if variant is None:raise NotFound('processing output unavailable')
        contract=processing_contract(connection,variant)
        if contract is None:raise NotFound('processing output unavailable')
        return asset,variant,contract

    def status(self,asset_id,*,principal):
        self._enabled()
        with self.database.transaction() as connection:
            asset,variant,contract=self._record(connection,asset_id,principal)
            states=processing_review_states(connection,variant)|{'privacy':review_state(connection,variant)}
            gate=FileService(self.router)._gate(connection,asset)
            return {'asset_id':asset_id,'revision':asset['revision'],'kind':'derived','parent_asset_id':asset['parent_asset_id'],
                'output_sha256':variant['output_sha256'],'recipe_hash':variant['recipe_hash'],'contract_version':CONTRACT_VERSION,
                'content_kind':contract.content_kind,'reviews':states,'asset_registered':True,
                'state':'available' if gate is None else 'registered_pending_review','file_access':gate or 'authorized',
                'output_download_available':gate is None,'medical_confirmation':False,'license_confirmation':False,
                'download_manifest':FileService._entry(asset_id,variant['output_sha256'],'download-manifest') if gate is None else None,
                'audio_voice_privacy_reviewed':False,'human_values_inherited':False,'time_map':contract.time_map.model_dump(mode='json')}

    def _review_access(self,connection,asset,principal):
        current,variant,contract=self._record(connection,asset['asset_id'],principal)
        reviewers=set(self.options.reviewers.quality)|set(self.options.reviewers.watermark)|set(self.options.reviewers.privacy)
        if principal.actor_id!=current['owner_id'] and principal.actor_id not in reviewers:
            raise AccessDenied('private processing output owner or designated reviewer required')
        self.catalog._authorize(connection,contract.parent_asset_id,Principal(actor_id=principal.actor_id))

    def review_files(self):return FileService(self.router,review_authorizer=self._review_access)

    def comparison(self,asset_id,*,principal):
        with self.database.transaction() as connection:
            asset,variant,contract=self._record(connection,asset_id,principal)
            self._review_access(connection,asset,principal)
            return {'asset_id':asset_id,'output_sha256':variant['output_sha256'],'source_sha256':contract.source_sha256,
                'recipe_hash':variant['recipe_hash'],'contract_version':CONTRACT_VERSION,'recipe':contract.recipe,
                'time_map':contract.time_map.model_dump(mode='json'),
                'source_preview':f'/processing/outputs/{asset_id}/source-content?sha256={contract.source_sha256}',
                'output_preview':f'/processing/outputs/{asset_id}/preview-content?sha256={variant["output_sha256"]}',
                'privacy_confirmation':False,'quality_confirmation':False}

    def open_source(self,asset_id,digest,*,principal):
        with self.database.transaction() as connection:
            asset,variant,contract=self._record(connection,asset_id,principal)
            self._review_access(connection,asset,principal)
            if digest!=contract.source_sha256:raise RevisionConflict('comparison source hash changed')
        return FileService(self.router).open(contract.parent_asset_id,digest,principal=Principal(actor_id=principal.actor_id))

    def review(self,asset_id,spec:OutputReview,*,principal):
        self._enabled()
        if not isinstance(spec,OutputReview):raise ValueError('strict processing review required')
        if principal.actor_id not in getattr(self.options.reviewers,spec.dimension):raise AccessDenied('explicit dimension reviewer required')
        if spec.decision=='approved':
            if not(spec.full_output_viewed and spec.source_compared):raise ValueError('full output and source comparison required')
            required={'quality':spec.no_unintended_changes,'watermark':spec.watermark_regions_checked,'privacy':spec.face_coverage_checked}
            if not required[spec.dimension]:raise ValueError('explicit dimension checks required')
            opened=self.review_files().open(asset_id,spec.output_sha256,principal=principal);opened.close()
            with self.database.transaction() as connection:
                current,variant,contract=self._record(connection,asset_id,principal,permission='edit')
            source_opened=self.open_source(asset_id,contract.source_sha256,principal=principal);source_opened.close()
        with self.database.transaction(write=True) as connection:
            asset,variant,contract=self._record(connection,asset_id,principal,permission='edit')
            if spec.decision=='approved':self._review_access(connection,asset,principal)
            revision=self.catalog._expected(asset,spec.expected_revision)
            if spec.output_sha256!=variant['output_sha256'] or spec.recipe_hash!=variant['recipe_hash'] or spec.contract_version!=contract.schema_version:
                raise RevisionConflict('review must bind actual output, recipe and contract version')
            required=spec.dimension=='quality' or spec.dimension=='watermark' and contract.review_requirements.watermark or spec.dimension=='privacy' and contract.review_requirements.privacy
            if not required:raise ValueError('review dimension is explicitly not applicable')
            if spec.decision=='approved' and self.review_files()._gate(connection,asset,review_asset_id=asset_id):
                raise RevisionConflict('producer or ancestor review unavailable')
            payload=encode(spec.model_dump(mode='json'));now=utc_now()
            if spec.dimension=='privacy':
                connection.execute('INSERT INTO privacy_reviews(asset_id,output_sha256,recipe_hash,decision,actor_id,asset_revision,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    (asset_id,spec.output_sha256,spec.recipe_hash,spec.decision,principal.actor_id,revision,payload,now))
            else:
                connection.execute('INSERT INTO processing_output_reviews(asset_id,dimension,output_sha256,recipe_hash,contract_version,decision,actor_id,asset_revision,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (asset_id,spec.dimension,spec.output_sha256,spec.recipe_hash,spec.contract_version,spec.decision,principal.actor_id,revision,payload,now))
            connection.execute('UPDATE assets SET revision=? WHERE asset_id=?',(revision,asset_id))
            self.catalog._event(connection,asset_id,revision,principal,'processing_'+spec.dimension+'_review',{},spec.model_dump(mode='json'))
        return self.status(asset_id,principal=principal)
