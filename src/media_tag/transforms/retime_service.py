"""CPU retiming service, durable via existing plans/jobs; no model or automatic privacy approval."""
import json
from hashlib import sha256
from media_tag.catalog import AccessDenied,NotFound,Principal,RevisionConflict
from media_tag.catalog.service import _human
from media_tag.contracts.models import Versions,WorkerResult,TaskEnvelope
from media_tag.storage import IntegrityError
from media_tag.router.models import JobSpec,StepSpec
from media_tag.variants.models import PlanSpec,Recipe,SourceSegment
from media_tag.variants.service import VariantService
from .models import RETIME_VERSION
from .policy import review_state,has_privacy_lineage
from .service import RedactionService,digest
from .retime import mapped_segments


class RetimeService(RedactionService):
    def __init__(self,router,tools,*,reviewers=()):
        self.router,self.database,self.catalog,self.registry=router,router.database,router.catalog,None
        self.variants=VariantService(router,tools,output_stages=('retime_render',))
        self.reviewers=frozenset(reviewers)

    def _needs_review(self,asset_id,principal):
        with self.database.transaction() as connection:
            return has_privacy_lineage(connection,self.catalog._authorize(connection,asset_id,principal))

    def start(self,spec,*,principal):
        _human(principal)
        segments=spec.segments
        if segments is None:
            with self.database.transaction() as connection:
                self.catalog._authorize(connection,spec.parent_asset_id,principal)
                previous=connection.execute('SELECT p.spec_json FROM variants v JOIN variant_plans p USING(plan_id) WHERE v.asset_id=?',(spec.parent_asset_id,)).fetchone()
            inherited=mapped_segments(json.loads(previous[0])) if previous else []
            segments=tuple(SourceSegment(segment_id=s['segment_id'],start_seconds=s['output_start_seconds'],end_seconds=s['output_end_seconds']) for s in inherited)
        recipe=Recipe(speed=spec.speed,audio=spec.audio,audio_stream_index=spec.audio_stream_index,segments=segments,
            privacy_review_required=self._needs_review(spec.parent_asset_id,principal),renderer_revision=RETIME_VERSION)
        plan=self.variants.create_plan(PlanSpec(parent_asset_id=spec.parent_asset_id,expected_revision=spec.expected_revision,
            recipe=recipe,chain_from_derived=spec.chain_from_derived),request_id='retime-plan-'+digest({'request':spec.request_id}),principal=principal)
        config=spec.options.model_dump()|{'variant_plan_id':plan['plan_id'],'variant_plan_hash':plan['recipe_hash']}
        job=self.router.submit(JobSpec(asset_id=spec.parent_asset_id,priority=1,steps=(StepSpec(key='retime',stage='retime_render',config=config,
            versions=Versions(rules_version=RETIME_VERSION,preprocess_version=RETIME_VERSION)),)),
            request_id='retime-'+digest({'request':spec.request_id}),principal=principal)
        return self.get(job['job_id'],principal=principal)

    def get(self,job_id,*,principal):
        status=self.router.get_job(job_id,principal=principal)
        if len(status['tasks'])!=1 or status['tasks'][0]['stage']!='retime_render': raise NotFound('not a retime job')
        with self.database.transaction() as connection:
            task=connection.execute('SELECT spec_json FROM router_tasks WHERE task_id=?',(status['tasks'][0]['task_id'],)).fetchone()
            config=json.loads(task[0])['config']
            variant=connection.execute('SELECT * FROM variants WHERE plan_id=?',(config['variant_plan_id'],)).fetchone()
            privacy=review_state(connection,variant) if variant else 'output_unavailable'
            access='output_unavailable'
            if variant:
                from media_tag.files.service import FileService
                asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(variant['asset_id'],)).fetchone()
                access=FileService(self.router)._gate(connection,asset) or 'authorized'
            report=None
            if status['state']=='succeeded':
                attempt=connection.execute('SELECT * FROM router_attempts WHERE task_id=? AND lease_epoch=?',
                    (status['tasks'][0]['task_id'],status['tasks'][0]['lease_epoch'])).fetchone()
                if attempt and attempt['released_at'] is not None:
                    result=WorkerResult.model_validate_json(attempt['result_json'])
                    envelope=TaskEnvelope.model_validate_json(attempt['envelope_json'])
                    self.router._artifacts(envelope,result)
                    refs=[r for r in result.artifacts if r.kind=='retime_report']
                    if len(refs)!=1: raise IntegrityError('one retime timing report required')
                    with self.router.files.open_read(refs[0].ref) as stream: raw=stream.read(32*1024**2+1)
                    if len(raw)>32*1024**2 or sha256(raw).hexdigest()!=refs[0].sha256: raise IntegrityError('timing report changed')
                    report=json.loads(raw)
        plan=self.variants.get_plan(config['variant_plan_id'],principal=principal)
        return {'job_id':job_id,'job':status,'plan_id':plan['plan_id'],'recipe_hash':plan['recipe_hash'],
            'source_sha256':plan['source_sha256'],'time_map':plan['contract']['time_map'],
            'cumulative_speed':plan['contract']['cumulative_speed'],'asset_id':variant['asset_id'] if variant else None,
            'privacy_review':privacy,'apply_transform_again':False,'segments':mapped_segments(plan['contract']),
            'report':report,'file_access':access,
            'output_reused':bool(variant and variant['task_id']!=status['tasks'][0]['task_id']),
            'adoption_state':'registered' if variant else 'pending_validation' if status['state']=='succeeded' else 'not_ready'}

    def adopt_ready(self):
        admin=Principal(actor_id='local-retime-adoption',role='admin')
        with self.database.transaction() as connection:
            attempts=connection.execute('''SELECT a.* FROM router_tasks t JOIN router_attempts a USING(task_id)
                JOIN router_jobs j USING(job_id) WHERE t.active=1 AND t.state='succeeded' AND j.cancelled=0
                AND t.lease_epoch=a.lease_epoch AND a.released_at IS NOT NULL
                AND json_extract(t.spec_json,'$.stage')='retime_render'
                AND NOT EXISTS (SELECT 1 FROM variants v WHERE v.plan_id=json_extract(a.config_json,'$.variant_plan_id'))''').fetchall()
        for attempt in attempts:
            result=WorkerResult.model_validate_json(attempt['result_json'])
            outputs=[a for a in result.artifacts if a.kind=='retimed_video']
            if len(outputs)!=1: raise RevisionConflict('one exact successful retime output required')
            config=json.loads(attempt['config_json'])
            self.variants.register_output(config['variant_plan_id'],outputs[0],task_id=attempt['task_id'],lease_epoch=attempt['lease_epoch'],principal=admin)

    def _review_access(self,connection,asset,principal):
        _human(principal); self.catalog._authorize(connection,asset['asset_id'],principal)
        row=connection.execute('''SELECT v.privacy_status,t.spec_json FROM variants v JOIN router_tasks t USING(task_id)
            WHERE v.asset_id=?''',(asset['asset_id'],)).fetchone()
        if row is None or row['privacy_status']!='pending' or json.loads(row['spec_json'])['stage']!='retime_render':
            raise AccessDenied('not a retimed privacy-gated output')
        if asset['owner_id']!=principal.actor_id and principal.actor_id not in self.reviewers:
            raise AccessDenied('owner or assigned reviewer required')
