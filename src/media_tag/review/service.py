"""CAS human review, exact-run evidence and private gap tracking."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4
from media_tag.catalog import AccessDenied, NotFound, RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.models import SourceInput
from media_tag.catalog.service import _human, encode
from media_tag.contracts.models import EffectiveValue, FieldValue, TaskEnvelope, WorkerResult
from media_tag.files.service import FileService, FileError
from media_tag.storage import IntegrityError
from .models import ReviewDeployment
from .policy import ASSETS, GAPS, MEDICAL, CONTROLLED, ReviewPolicy, basis, index_status


class ReviewService:
    def __init__(self,router,config=None):
        self.router,self.catalog,self.database=router,router.catalog,router.database
        self.config=config or ReviewDeployment(enabled=True)
        self.policy=ReviewPolicy(self.catalog.contract,self.config)
        self.catalog.review_policy=self.policy
        self.files=FileService(router)

    def _asset(self,connection,asset_id,principal,sha=None,revision=None):
        _human(principal)
        asset=self.catalog._authorize(connection,asset_id,principal,'edit')
        if sha is not None and asset['blob_sha256']!=sha:raise RevisionConflict('source file changed')
        if revision is not None:self.catalog._expected(asset,revision)
        return asset

    def _previous(self,connection,principal,spec,scope):
        fingerprint=sha256(encode({'scope':scope,'spec':spec.model_dump(mode='json')}).encode()).hexdigest()
        row=connection.execute('SELECT * FROM review_requests WHERE actor_id=? AND request_id=?',(principal.actor_id,spec.request_id)).fetchone()
        if row and row['fingerprint']!=fingerprint:raise RevisionConflict('request ID reused with different input')
        return fingerprint,json.loads(row['result_json']) if row else None

    def _remember(self,connection,principal,spec,fingerprint,result):
        connection.execute('INSERT INTO review_requests VALUES(?,?,?,?)',(principal.actor_id,spec.request_id,fingerprint,encode(result)))
        return result

    def _medical(self,principal):
        if principal.actor_id not in self.config.reviewers:raise AccessDenied('explicit medical reviewer binding required')

    def observe(self,asset_id,spec,*,principal):
        _human(principal)
        if spec.reference:SourceInput(reference=spec.reference)
        if spec.kind!='human_record':
            if not spec.viewed:raise ValueError('explicit actual observation required')
            opened=self.files.open(asset_id,spec.source_sha256,principal=principal)
            mime=opened.mime;opened.close()
            if spec.kind=='image' and not mime.startswith('image/'):raise ValueError('image observation requires image')
        with self.database.transaction(write=True) as connection:
            asset=self._asset(connection,asset_id,principal,spec.source_sha256)
            fingerprint,previous=self._previous(connection,principal,spec,'observation:'+asset_id)
            if previous:return previous
            self.catalog._expected(asset,spec.expected_revision)
            if spec.kind in ('video_segment','audio_segment'):
                technical=self.files._technical(connection,asset)
                duration=technical['time_basis']['duration_seconds'] or technical['duration_seconds']
                if technical['kind'] in ('video','audio') and (technical['time_basis']['normalization_origin_seconds'] is None or technical['time_basis']['duration_seconds'] is None):
                    raise ValueError('continuous observation requires a known common source clock')
                if spec.kind=='video_segment' and not (mime.startswith('video/') or technical['kind'] in ('gif','animation')) or spec.kind=='audio_segment' and not (mime.startswith('audio/') or technical['has_audio']):
                    raise ValueError('observation media kind mismatch')
                if duration is None or spec.start_seconds is None or spec.end_seconds is None or not spec.start_seconds<spec.end_seconds<=duration:
                    raise ValueError('bounded interval in current file clock required')
            elif spec.start_seconds is not None or spec.end_seconds is not None:raise ValueError('unexpected observation interval')
            observation_id='human-'+uuid4().hex
            data=spec.model_dump(mode='json')|{'actor_id':principal.actor_id,'source_sha256':asset['blob_sha256']}
            connection.execute('INSERT INTO review_observations VALUES(?,?,?,?,?,?)',(observation_id,asset_id,asset['blob_sha256'],principal.actor_id,encode(data),utc_now()))
            return self._remember(connection,principal,spec,fingerprint,{'observation_id':observation_id,'source_sha256':asset['blob_sha256']})

    def observations(self,connection,asset_id,ids,source_sha):
        if not ids or len(set(ids))!=len(ids):raise ValueError('explicit unique observations required')
        records=[]
        for identifier in ids:
            row=connection.execute('SELECT * FROM review_observations WHERE observation_id=? AND asset_id=?',(identifier,asset_id)).fetchone()
            if not row or row['source_sha256']!=source_sha:raise RevisionConflict('observation source mismatch')
            records.append(json.loads(row['payload_json']))
        return records

    def suggestion(self,connection,asset,run_id,suggestion_id):
        row=connection.execute('''SELECT s.payload,r.task_json,r.result_json FROM tag_suggestions s JOIN suggestion_runs r USING(run_id)
            WHERE s.asset_id=? AND s.run_id=? AND s.suggestion_id=?''',(asset['asset_id'],run_id,suggestion_id)).fetchone()
        if row is None:raise NotFound('suggestion unavailable')
        task=TaskEnvelope.model_validate_json(row['task_json']);result=WorkerResult.model_validate_json(row['result_json'])
        live=connection.execute('''SELECT t.active,t.state,t.lease_epoch,j.cancelled,a.released_at FROM router_tasks t
            JOIN router_jobs j USING(job_id) JOIN router_attempts a USING(task_id) WHERE t.task_id=? AND a.lease_epoch=?''',(task.task_id,task.lease_epoch)).fetchone()
        if task.input_refs[0].sha256!=asset['blob_sha256'] or live is None or not live['active'] or live['state']!='succeeded' or live['cancelled'] or live['lease_epoch']!=task.lease_epoch or live['released_at'] is None:
            raise RevisionConflict('suggestion producer or source is no longer current')
        self.router._artifacts(task,result)
        from media_tag.contracts.models import TagSuggestion
        return TagSuggestion.model_validate_json(row['payload']),task,result

    def decide(self,asset_id,spec,*,principal):
        with self.database.transaction(write=True) as connection:
            asset=self._asset(connection,asset_id,principal,spec.source_sha256)
            fingerprint,previous=self._previous(connection,principal,spec,'field:'+asset_id)
            if previous:return previous
            revision=self.catalog._expected(asset,spec.expected_revision)
            definition=self.policy.asset_fields.get(spec.field)
            if not definition or definition.fill_policy=='script' or spec.field in CONTROLLED:raise ValueError('field is controlled by a dedicated workflow')
            if spec.field in MEDICAL or definition.fill_policy=='ai_candidate_per_field_human_review':self._medical(principal)
            suggestion=None
            if spec.decision in ('accept','reject'):
                if not spec.run_id or not spec.suggestion_id or spec.value is not None:raise ValueError('exact suggestion selection required')
                suggestion,_,_=self.suggestion(connection,asset,spec.run_id,spec.suggestion_id)
                if suggestion.field!=spec.field:raise ValueError('suggestion field mismatch')
            elif spec.value is None or spec.run_id or spec.suggestion_id:raise ValueError('manual value required')
            value=spec.value if spec.decision=='manual' else suggestion.value
            old=connection.execute('SELECT payload FROM effective_values WHERE asset_id=? AND field=?',(asset_id,spec.field)).fetchone()
            before_values=[json.loads(old[0])] if old else []
            effective=None
            if spec.decision!='reject':
                observations=self.observations(connection,asset_id,spec.observation_ids,asset['blob_sha256'])
                self.policy.validate(definition,value)
                if (spec.field in MEDICAL or definition.fill_policy=='ai_candidate_per_field_human_review') and value.state=='observed':
                    if not any(o['kind'] in ('image','video_segment','audio_segment') and o['viewed'] and o['actor_id']==principal.actor_id for o in observations):raise ValueError('medical value requires actual media observation')
                    if (spec.dynamic or spec.field=='过程/状态' and any('→' in v or '->' in v for v in value.values)) and not any(o['kind']=='video_segment' and o['viewed'] and o['actor_id']==principal.actor_id for o in observations):raise ValueError('process requires continuous video observation')
                if spec.field in ('来源','原始页面','许可状态','署名文本') and value.state=='observed' and not any(o['kind']=='human_record' for o in observations):
                    raise ValueError('source and license fields require traceable human record')
                if spec.field=='许可状态' and value.state=='observed' and value.values[0]!='未核实' and not any(o['reference'] for o in observations if o['kind']=='human_record'):
                    raise ValueError('license attestation requires a traceable original page')
                if spec.field=='含人脸' and value.state=='observed':
                    from media_tag.transforms.policy import review_state
                    variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(asset_id,)).fetchone()
                    processed=bool(variant and variant['privacy_status']=='pending' and review_state(connection,variant)=='approved' and self.files._gate(connection,asset) is None)
                    if value.values[0]=='已处理副本' and not processed or processed and value.values[0]!='已处理副本':
                        raise ValueError('processed face label must bind approved output')
                    if not processed and not any(o['kind'] in ('image','video_segment') and o['viewed'] and o['actor_id']==principal.actor_id for o in observations):
                        raise ValueError('face labels require actual visual observation')
                    technical=self.files._technical(connection,asset)
                    duration=technical['time_basis']['duration_seconds'] or technical['duration_seconds']
                    if value.values[0]=='无' and technical['kind']=='video' and not any(o['kind']=='video_segment' and o['viewed'] and o['actor_id']==principal.actor_id and o['start_seconds']<=.001 and duration is not None and o['end_seconds']>=duration-.001 for o in observations):
                        raise ValueError('absence of faces in a video requires full continuous observation')
                effective=EffectiveValue(field=spec.field,value=value,actor_id=principal.actor_id,revision=revision,
                    evidence_ids=spec.observation_ids,confirmed_at=datetime.now(timezone.utc))
                connection.execute('''INSERT INTO effective_values VALUES(?,?,?) ON CONFLICT(asset_id,field) DO UPDATE SET payload=excluded.payload''',(asset_id,spec.field,effective.model_dump_json()))
            identifier='decision-'+uuid4().hex
            payload=spec.model_dump(mode='json')|{'effective':effective.model_dump(mode='json') if effective else None,'policy_sha256':self.policy.digest}
            connection.execute('INSERT INTO review_decisions VALUES(?,?,?,?,?,?,?,?,?)',(identifier,asset_id,asset['blob_sha256'],spec.field,spec.decision,principal.actor_id,revision,encode(payload),utc_now()))
            connection.execute('UPDATE assets SET revision=? WHERE asset_id=?',(revision,asset_id))
            self.catalog._event(connection,asset_id,revision,principal,'field_review',{'values':before_values},
                {'values':[payload['effective']] if effective else before_values,'review':payload})
            return self._remember(connection,principal,spec,fingerprint,{'decision_id':identifier,'revision':revision,'effective':payload['effective'],'medical_final_review':False})

    def confirm(self,asset_id,spec,*,principal):
        self._medical(principal)
        if spec.decision=='confirm':
            opened=self.files.open(asset_id,spec.source_sha256,principal=principal);opened.close()
        with self.database.transaction(write=True) as connection:
            asset=self._asset(connection,asset_id,principal,spec.source_sha256)
            fingerprint,previous=self._previous(connection,principal,spec,'confirmation:'+asset_id)
            if previous:return previous
            revision=self.catalog._expected(asset,spec.expected_revision)
            if spec.decision=='confirm':
                if not spec.full_source_viewed:raise ValueError('full source observation must be explicit')
                if self.files._gate(connection,asset):raise RevisionConflict('privacy or producer gate prevents index confirmation')
                technical=self.files._technical(connection,asset)
                observed=[json.loads(r[0]) for r in connection.execute('SELECT payload_json FROM review_observations WHERE asset_id=? AND actor_id=? AND source_sha256=?',(asset_id,principal.actor_id,asset['blob_sha256']))]
                duration=technical['time_basis']['duration_seconds'] or technical['duration_seconds']
                if not any(o['viewed'] and (o['kind']=='image' and technical['kind'] in ('image','gif','animation') or
                    o['kind'] in ('video_segment','audio_segment') and duration is not None and o['start_seconds']<=.001 and o['end_seconds']>=duration-.001) for o in observed):
                    raise ValueError('reviewer must record full source observation')
                values={r['field']:EffectiveValue.model_validate_json(r['payload']) for r in connection.execute('SELECT * FROM effective_values WHERE asset_id=?',(asset_id,))}
                for field,definition in self.policy.asset_fields.items():
                    if definition.fill_policy=='script' or field in CONTROLLED:continue
                    value=values.get(field)
                    if value is None or value.value.state not in ('observed','not_applicable'):raise ValueError('every reviewable field must be explicitly resolved')
                    self.policy.validate(definition,value.value)
                    record=connection.execute("SELECT payload_json FROM review_decisions WHERE asset_id=? AND field=? AND asset_revision=? AND decision!='reject'",(asset_id,field,value.revision)).fetchone()
                    if not record or json.loads(record[0])['effective']!=value.model_dump(mode='json') or json.loads(record[0])['policy_sha256']!=self.policy.digest:raise RevisionConflict('current effective value lacks matching D06 review')
            digest=basis(connection,asset,self.policy.digest)
            connection.execute('INSERT INTO index_confirmations(asset_id,source_sha256,basis_sha256,policy_sha256,decision,actor_id,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (asset_id,asset['blob_sha256'],digest,self.policy.digest,spec.decision,principal.actor_id,encode(spec.model_dump(mode='json')),utc_now()))
            connection.execute('UPDATE assets SET revision=? WHERE asset_id=?',(revision,asset_id))
            self.catalog._event(connection,asset_id,revision,principal,'index_review',{},spec.model_dump(mode='json'))
            return self._remember(connection,principal,spec,fingerprint,{'asset_id':asset_id,'revision':revision,'index_status':'confirmed' if spec.decision=='confirm' else 'needs_review','privacy_approved':False,'current_use_review':'not_checked'})

    def detail(self,asset_id,*,principal):
        with self.database.transaction() as connection:
            asset=self._asset(connection,asset_id,principal)
            snapshot=self.catalog._snapshot(connection,asset)
            suggestions=[]
            for row in connection.execute('SELECT * FROM tag_suggestions WHERE asset_id=? ORDER BY created_at,suggestion_id',(asset_id,)):
                item=json.loads(row['payload'])|{'run_id':row['run_id'],'base_revision':row['base_revision'],'available':True,'evidence':[]}
                try:
                    _,task,result=self.suggestion(connection,asset,row['run_id'],row['suggestion_id'])
                    item['versions']=task.versions.model_dump(mode='json')
                    item['evidence']=[{'evidence_id':e.evidence_id,'kind':e.kind,'start_seconds':e.start_seconds,'end_seconds':e.end_seconds,
                        'sha256':e.artifact.sha256,'content':f'/review/assets/{asset_id}/evidence/{row["run_id"]}/{e.evidence_id}?sha256={e.artifact.sha256}'} for e in result.evidence if e.evidence_id in item['evidence_ids']]
                except (RevisionConflict,ValueError,IntegrityError,OSError):item['available']=False
                item['decisions']=[json.loads(r[0]) for r in connection.execute('SELECT payload_json FROM review_decisions WHERE asset_id=? AND json_extract(payload_json,\'$.run_id\')=? AND json_extract(payload_json,\'$.suggestion_id\')=?',(asset_id,row['run_id'],row['suggestion_id']))]
                suggestions.append(item)
            return {'asset_id':asset_id,'sha256':asset['blob_sha256'],'revision':asset['revision'],'name':asset['name'],
                'index_status':index_status(connection,asset,self.policy),'medical_reviewer':principal.actor_id in self.config.reviewers,
                'effective_values':[v.model_dump(mode='json') for v in snapshot.effective_values], 'suggestions':suggestions,
                'decisions':[{'decision_id':r['decision_id'],'field':r['field'],'decision':r['decision'],'actor_id':r['actor_id'],
                    'asset_revision':r['asset_revision'],'created_at':r['created_at'],'data':json.loads(r['payload_json'])}
                    for r in connection.execute('SELECT * FROM review_decisions WHERE asset_id=? ORDER BY asset_revision',(asset_id,))],
                'observations':[dict(observation_id=r['observation_id'],**json.loads(r['payload_json'])) for r in connection.execute('SELECT * FROM review_observations WHERE asset_id=? ORDER BY created_at',(asset_id,))],
                'fields':[{'name':d.name,'type':d.field_type,'policy':d.fill_policy,'medical':d.name in MEDICAL or d.fill_policy=='ai_candidate_per_field_human_review',
                    'editable':d.fill_policy!='script' and d.name not in CONTROLLED,'options':self.policy.options(d),'options_available':d.selection_policy!='external_vocabulary' or d.name in self.policy.taxonomy} for d in self.policy.asset_fields.values()],
                'source_records':[dict(r) for r in connection.execute('''SELECT source_id,label,reference,attribution,license_status FROM asset_sources WHERE asset_id=? OR source_id IN (SELECT source_id FROM variant_source_links WHERE asset_id=?) ORDER BY source_id''',(asset_id,asset_id))],
                'policy_sha256':self.policy.digest,'technical':self.files._technical(connection,asset)}

    def _gap(self,connection,gap_id,principal):
        _human(principal)
        row=connection.execute('SELECT * FROM catalog_gaps WHERE gap_id=?',(gap_id,)).fetchone()
        if not row:raise NotFound('gap unavailable')
        if row['owner_id']!=principal.actor_id and principal.role!='admin':raise AccessDenied('private gap scope required')
        return row

    def _gap_values(self,values,principal):
        for field,value in values.items():
            definition=self.policy.gap_fields.get(field)
            if not definition or field in ('缺口ID','出现次数','首次提出日期','提出人'):raise ValueError('gap field is controlled')
            if field in MEDICAL and value.state=='observed':self._medical(principal)
            self.policy.validate(definition,value,gap=True)
        return {field:value.model_dump(mode='json') for field,value in values.items()}

    def _gap_links(self,connection,values,principal):
        links=values.get('补上的素材',{}).get('values') or []
        if values.get('处理状态',{}).get('values')==['已补'] and not links:raise ValueError('resolved gap requires linked assets')
        for asset_id in links:
            asset=self.catalog._authorize(connection,asset_id,principal)
            if self.files._gate(connection,asset):raise RevisionConflict('linked asset is privacy gated')

    @staticmethod
    def _gap_identity(values):
        import unicodedata
        def normalize(x):
            if isinstance(x,str):return ' '.join(unicodedata.normalize('NFKC',x).split())
            if isinstance(x,list):return [normalize(v) for v in x]
            if isinstance(x,dict):return {k:normalize(v) for k,v in x.items()}
            return x
        identity={k:v for k,v in values.items() if k not in ('处理状态','补上的素材','已找过的地方')}
        return sha256(encode(normalize(identity)).encode()).hexdigest()

    def gap_create(self,spec,*,principal):
        _human(principal);values=self._gap_values(spec.values,principal)
        if values.get('需要的画面',{}).get('state')!='observed':raise ValueError('explicit requested visual required')
        dedup=self._gap_identity(values)
        with self.database.transaction(write=True) as connection:
            fingerprint,previous=self._previous(connection,principal,spec,'gap-create')
            if previous:return previous
            self._gap_links(connection,values,principal)
            row=connection.execute('SELECT * FROM catalog_gaps WHERE owner_id=? AND fingerprint=?',(principal.actor_id,dedup)).fetchone()
            if row:
                gap_id=row['gap_id'];revision=row['revision']+1
                connection.execute('UPDATE catalog_gaps SET revision=?,occurrences=occurrences+1 WHERE gap_id=?',(revision,gap_id))
            else:
                gap_id='G-'+uuid4().hex;revision=1
                values.setdefault('处理状态',{'state':'observed','values':['待补'],'reason':None})
                connection.execute('INSERT INTO catalog_gaps VALUES(?,?,?,?,?,1,?)',(gap_id,principal.actor_id,revision,dedup,encode(values),utc_now()))
            connection.execute('INSERT INTO gap_events(gap_id,actor_id,revision,payload_json,created_at) VALUES(?,?,?,?,?)',(gap_id,principal.actor_id,revision,encode(spec.model_dump(mode='json')),utc_now()))
            return self._remember(connection,principal,spec,fingerprint,{'gap_id':gap_id,'revision':revision})

    def gap_revise(self,gap_id,spec,*,principal):
        values=self._gap_values(spec.values,principal)
        with self.database.transaction(write=True) as connection:
            row=self._gap(connection,gap_id,principal)
            fingerprint,previous=self._previous(connection,principal,spec,'gap-update:'+gap_id)
            if previous:return previous
            if row['revision']!=spec.expected_revision:raise RevisionConflict('gap revision changed')
            current=json.loads(row['values_json'])|values;self._gap_links(connection,current,principal)
            if current.get('需要的画面',{}).get('state')!='observed':raise ValueError('gap must retain its explicit requested visual')
            identity=self._gap_identity(current)
            collision=connection.execute('SELECT gap_id FROM catalog_gaps WHERE owner_id=? AND fingerprint=? AND gap_id!=?',(row['owner_id'],identity,gap_id)).fetchone()
            if collision:raise RevisionConflict('another gap already has this identity')
            connection.execute('UPDATE catalog_gaps SET revision=revision+1,values_json=?,fingerprint=? WHERE gap_id=?',(encode(current),identity,gap_id))
            connection.execute('INSERT INTO gap_events(gap_id,actor_id,revision,payload_json,created_at) VALUES(?,?,?,?,?)',(gap_id,principal.actor_id,row['revision']+1,encode(spec.model_dump(mode='json')),utc_now()))
            return self._remember(connection,principal,spec,fingerprint,{'gap_id':gap_id,'revision':row['revision']+1})

    def gaps(self,*,principal):
        _human(principal)
        with self.database.transaction() as connection:
            return {'items':[dict(r)|{'values':json.loads(r['values_json'])} for r in connection.execute('SELECT gap_id,owner_id,revision,values_json,occurrences,created_at FROM catalog_gaps WHERE owner_id=? ORDER BY created_at DESC LIMIT 200',(principal.actor_id,))],
                'fields':[{'name':d.name,'type':d.field_type,'options':self.policy.options(d),'controlled':d.name in ('缺口ID','出现次数','首次提出日期','提出人')} for d in self.policy.gap_fields.values()]}
