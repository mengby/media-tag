"""Project only actual, current, hash-verified model observations into AI drafts."""
from hashlib import sha256
import json
import math
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import encode
from media_tag.contracts.models import TaskEnvelope,WorkerResult,ArtifactRef
from media_tag.storage import StorageError

STAGES=('ocr','face','clip')
VERSION='ai-observations-v2'


class _ReadView:
    """One transaction's current tasks/projections; never reused between requests."""
    def __init__(self,connection):self.connection=connection;self.rows={};self.documents={}


def vector(value):
    if not isinstance(value,list) or len(value)!=512 or any(type(v) not in (int,float) or not math.isfinite(v) for v in value):raise ValueError('invalid visual vector')
    norm=math.sqrt(sum(v*v for v in value))
    if not .99<=norm<=1.01:raise ValueError('visual vector is not normalized')
    return [float(v)/norm for v in value]


class DraftIndex:
    def __init__(self,router):self.router,self.database=router,router.database

    def read_view(self,c,assets):
        view=_ReadView(c);ids=[a['asset_id'] for a in assets]
        for start in range(0,len(ids),400):
            group=ids[start:start+400];marks=','.join('?' for _ in group)
            for asset_id in group:view.rows[asset_id]=[]
            rows=c.execute('''SELECT j.asset_id,t.task_id,t.job_id,t.step_key,t.lease_epoch,t.rowid AS task_order,j.cancelled AS job_cancelled,
                t.state,t.spec_json,a.envelope_json,a.result_json,a.result_digest,a.released_at
                FROM router_tasks t JOIN router_jobs j USING(job_id)
                LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                WHERE j.asset_id IN ('''+marks+''') AND t.active=1
                AND (j.cancelled=0 OR json_extract(t.spec_json,'$.stage') IN ('visual_fields','precision','local_topics','audio_topics'))
                AND json_extract(t.spec_json,'$.stage') IN ('ocr','face','clip','visual_fields','precision','local_topics','audio_topics')
                ORDER BY j.created_at DESC,t.rowid DESC''',group)
            for row in rows:view.rows[row['asset_id']].append(row)
            for row in c.execute('SELECT asset_id,stage,basis_sha256,payload_json FROM ai_draft_documents WHERE asset_id IN ('+marks+')',group):
                view.documents[(row['asset_id'],row['stage'],row['basis_sha256'])]=row['payload_json']
        return view

    @staticmethod
    def _report_ref(task,result):
        refs=[r for r in result.artifacts if r.kind==('visual_fields_report' if task.stage=='visual_fields' else 'model_report')]
        if len(refs)!=1:raise ValueError('one model report required')
        return refs[0]

    def _report(self,task,result):
        ref=self._report_ref(task,result)
        self.router._artifacts(task,result)
        for evidence in result.evidence:
            with self.router.files.open_read(evidence.artifact.ref) as stream:frame=stream.read(16*1024**2+1)
            if len(frame)>16*1024**2 or sha256(frame).hexdigest()!=evidence.artifact.sha256:raise ValueError('frame integrity')
        with self.router.files.open_read(ref.ref) as stream:raw=stream.read(8*1024**2+1)
        if len(raw)>8*1024**2 or sha256(raw).hexdigest()!=ref.sha256:raise ValueError('report integrity')
        report=json.loads(raw)
        if report.get('kind')!=task.stage or report.get('source_sha256')!=task.input_refs[0].sha256 or report.get('model_revision')!=task.versions.model_revision:
            raise ValueError('report identity')
        frames=[report['frame']] if task.stage=='visual_fields' else report.get('frames',[])
        if not frames or len(frames)>12:raise ValueError('invalid frames')
        return ref,report

    def _project(self,task,result,ref,report):
        labels=[];features=[]
        if task.stage=='visual_fields':
            from media_tag.semantic.contracts import Caption
            from media_tag.semantic.runtime import suggestions
            from media_tag.semantic.prompt import PROMPT_VERSION
            caption=Caption.model_validate_json(json.dumps(report['caption']))
            frame=report['frame'];artifact=ArtifactRef.model_validate(frame['artifact'])
            evidence=next((e for e in result.evidence if e.artifact==artifact and e.source_sha256==report['source_sha256'] and e.start_seconds==frame['normalized_seconds']),None)
            if evidence is None or artifact not in task.input_refs or report['prompt_version']!=PROMPT_VERSION or result.suggestions!=suggestions(caption,task,evidence.evidence_id):raise ValueError('semantic report binding')
            for item in result.suggestions:
                if item.value.state!='observed':continue
                labels.append({'draft_id':item.suggestion_id,'kind':'field_suggestion','field':item.field,'value':item.value.values[0],
                    'text':item.value.values[0],'state':'ai_draft','confidence':None,'score_kind':'generative_draft_not_probability',
                    'task_id':task.task_id,'lease_epoch':task.lease_epoch,'model_revision':report['model_revision'],
                    'preprocess_version':task.versions.preprocess_version,'prompt_version':report['prompt_version'],
                    'report_sha256':ref.sha256,'frame_sha256':artifact.sha256,'evidence_id':evidence.evidence_id,
                    'start_seconds':frame['normalized_seconds'],'medical_confirmation':False,'synonym_mapping_published':False})
            field_states={s.field:s.value.model_dump(mode='json') for s in result.suggestions}
            normalizations=list(report.get('normalizations',[]))
            if 'form_outside_template_left_unknown' in normalizations:
                field_states['表现形式']={'state':'no_match','values':None,'reason':'model_candidate_outside_original_options'}
            return {'schema_version':VERSION,'stage':task.stage,'source_sha256':report['source_sha256'],'producer_status':result.status,
                'labels':labels,'features':[],'field_states':field_states,'normalizations':normalizations,
                'provenance':{'task_id':task.task_id,'lease_epoch':task.lease_epoch,'model_revision':report['model_revision'],
                    'prompt_version':report['prompt_version'],'preprocess_version':task.versions.preprocess_version,
                    'report_sha256':ref.sha256,'frame_sha256':artifact.sha256,'evidence_id':evidence.evidence_id},
                'uncertainties':list(caption.uncertainties),'model_revision':report['model_revision'],
                'medical_confirmation':False,'privacy_confirmation':False,'license_confirmation':False}
        for number,frame in enumerate(report['frames']):
            artifact=ArtifactRef.model_validate(frame['artifact']);seconds=frame.get('normalized_seconds')
            evidence=next((e for e in result.evidence if e.artifact==artifact and e.source_sha256==report['source_sha256'] and e.start_seconds==seconds),None)
            if evidence is None or artifact not in task.input_refs:raise ValueError('unbound model frame')
            observation=frame['observation']
            provenance={'task_id':task.task_id,'lease_epoch':task.lease_epoch,'model_revision':report['model_revision'],
                'preprocess_version':task.versions.preprocess_version,'report_sha256':ref.sha256,
                'evidence_id':evidence.evidence_id,'frame_sha256':artifact.sha256,'start_seconds':seconds}
            def label(kind,text,index,score=None,**extra):
                labels.append({'draft_id':f"draft-{sha256(encode([task.task_id,task.lease_epoch,ref.sha256,number,index,kind]).encode()).hexdigest()[:32]}",
                    'kind':kind,'text':text,'state':'ai_draft','medical_confirmation':False,
                    'confidence':score,'score_kind':'model_score_not_medical_probability',**provenance,**extra})
            if task.stage=='ocr':
                lines=observation.get('lines',[])
                if len(lines)>1024:raise ValueError('OCR line budget')
                for index,line in enumerate(lines):
                    text=line['text'];score=line['recognition_score'];box=line['quadrilateral']
                    if not isinstance(text,str) or not 0<len(text)<=2000 or type(score) not in (float,int) or not math.isfinite(score) or not 0<=score<=1:raise ValueError('invalid OCR line')
                    if len(box)!=4 or any(len(p)!=2 or any(type(v) not in (int,float) or not math.isfinite(v) for v in p) for p in box):raise ValueError('invalid OCR bounds')
                    label('ocr_text',text,index,float(score),quadrilateral=box,field=None)
            elif task.stage=='face':
                faces=observation.get('faces',[])
                if not isinstance(faces,list) or len(faces)>1000:raise ValueError('invalid face observation')
                for face in faces:
                    box=face['box_xyxy'];score=face['detection_score']
                    if len(box)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) for v in box) or not box[2]>box[0] or not box[3]>box[1] or type(score) not in (int,float) or not math.isfinite(score) or not 0<=score<=1:raise ValueError('invalid face bounds')
                if observation.get('state')!=('observed' if faces else 'no_detection_in_sample'):raise ValueError('face state mismatch')
                label('face_observation','检测到人脸，需核查打码' if faces else '抽样帧未检出人脸',0,
                    max((f['detection_score'] for f in faces),default=None),count=len(faces),
                    field='含人脸' if faces else None,value='有-需打码' if faces else None,
                    redaction_completed=False,full_video_observed=False)
            else:
                features.append({**provenance,'embedding':vector(observation['embedding']),
                    'candidate_state':report.get('candidate_state'),'medical_confirmation':False})
        if sum(len(l['text']) for l in labels)>128*1024:raise ValueError('draft text budget')
        return {'schema_version':VERSION,'stage':task.stage,'source_sha256':report['source_sha256'],
            'producer_status':result.status,'labels':labels,'features':features,'model_revision':report['model_revision'],
            'classification_state':'missing_published_vocabulary' if task.stage=='clip' else 'not_generated',
            'medical_confirmation':False,'privacy_confirmation':False,'license_confirmation':False}

    def sync(self,c,asset,*,persist=True,select_document=None,read_view=None):
        # A search prefilter can only exclude a current indexed nonmatch. Every
        # returned candidate still goes through the same report/frame byte checks.
        if select_document is not None and persist:raise ValueError('search selection must be read-only')
        if read_view is not None and (read_view.connection is not c or persist):raise ValueError('read view requires its original read transaction')
        rows=read_view.rows[asset['asset_id']] if read_view is not None else c.execute('''SELECT t.task_id,t.job_id,t.step_key,t.lease_epoch,t.rowid AS task_order,j.cancelled AS job_cancelled,
            t.state,t.spec_json,a.envelope_json,a.result_json,a.result_digest,a.released_at
            FROM router_tasks t JOIN router_jobs j USING(job_id)
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE j.asset_id=? AND t.active=1 AND (j.cancelled=0 OR json_extract(t.spec_json,'$.stage') IN ('visual_fields','precision','local_topics','audio_topics'))
            AND json_extract(t.spec_json,'$.stage') IN ('ocr','face','clip','visual_fields','precision','local_topics','audio_topics')
            ORDER BY j.created_at DESC,t.rowid DESC''',(asset['asset_id'],)).fetchall()
        latest={}
        for row in rows:
            if json.loads(row['spec_json'])['stage'] not in ('precision','local_topics','audio_topics'):latest.setdefault(json.loads(row['spec_json'])['stage'],row)
        documents=[];states={}
        stages=(*STAGES,'visual_fields') if 'visual_fields' in latest or 'visual_fields' in self.router.stages else STAGES
        for stage in stages:
            row=latest.get(stage)
            state='not_requested' if row is None else row['state'];document=None
            if row and row['job_cancelled']:
                states[stage]='cancelled'
                if persist:c.execute('DELETE FROM ai_draft_documents WHERE asset_id=? AND stage=?',(asset['asset_id'],stage))
                continue
            if row and row['released_at'] is not None and row['result_json']:
                try:
                    task=TaskEnvelope.model_validate_json(row['envelope_json']);result=WorkerResult.model_validate_json(row['result_json'])
                    if task.stage!=stage or task.input_refs[0].sha256!=asset['blob_sha256']:raise ValueError('source changed')
                    if result.status=='not_applicable' and row['state']=='succeeded':
                        self.router._artifacts(task,result)
                        document={'stage':stage,'labels':[],'features':[]}
                        if stage=='visual_fields':
                            from media_tag.semantic.contracts import FIELDS
                            document.update(field_states={f:{'state':'not_applicable','values':None,'reason':result.reason} for f in FIELDS},uncertainties=[],normalizations=[])
                        documents.append(document);states[stage]='not_applicable'
                        if persist:c.execute('DELETE FROM ai_draft_documents WHERE asset_id=? AND stage=?',(asset['asset_id'],stage))
                        continue
                    usable=result.status=='succeeded' and row['state']=='succeeded' or (stage=='clip' and result.status=='partial' and row['state']=='needs_attention' and result.errors and all(e.code=='unpublished_vocabulary' for e in result.errors))
                    if not usable:raise ValueError('model result unavailable')
                    ref=self._report_ref(task,result)
                    basis=sha256(encode([VERSION,asset['blob_sha256'],task.task_id,task.lease_epoch,row['result_digest'],ref.sha256]).encode()).hexdigest()
                    if read_view is None:
                        old=c.execute('SELECT payload_json FROM ai_draft_documents WHERE asset_id=? AND stage=? AND basis_sha256=?',(asset['asset_id'],stage,basis)).fetchone()
                        cached=old[0] if old else None
                    else:cached=read_view.documents.get((asset['asset_id'],stage,basis))
                    indexed=json.loads(cached) if cached is not None else None
                    if indexed is not None and select_document is not None and not select_document(indexed):
                        states[stage]='not_selected'
                        continue
                    # No index/version match -> full verification and projection.
                    # A matching cached candidate is never trusted without bytes.
                    ref,report=self._report(task,result)
                    document=indexed if indexed is not None else self._project(task,result,ref,report)
                    if persist and cached is None:c.execute('''INSERT INTO ai_draft_documents VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id,stage) DO UPDATE SET
                        source_sha256=excluded.source_sha256,basis_sha256=excluded.basis_sha256,payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
                        (asset['asset_id'],stage,asset['blob_sha256'],basis,encode(document),utc_now()))
                    state='ready' if stage!='clip' else 'visual_features_ready_classification_missing'
                except (StorageError,OSError,ValueError,KeyError,TypeError,StopIteration):state='unavailable'
            if document is not None and (select_document is None or select_document(document)):documents.append(document)
            elif persist:c.execute('DELETE FROM ai_draft_documents WHERE asset_id=? AND stage=?',(asset['asset_id'],stage))
            states[stage]=state
        from media_tag.topic_index.service import TopicIndex
        topics=[]
        for topic_stage in ('local_topics','audio_topics','precision'):
            topic,claimed=TopicIndex(self.router,stage=topic_stage).project(c,asset,persist=persist,rows=rows,
                cached_documents=read_view.documents if read_view is not None else None,select_document=select_document)
            if topic is not None:
                # Stronger current scopes mask older successes even while pending,
                # failed, cancelled, rejected or damaged. Human values remain separate.
                for document in documents:
                    document['labels']=[label for label in document['labels'] if label.get('field') not in claimed]
                states[topic_stage]=topic['state'] if topic['state']=='not_selected' else (
                    'ready' if topic['labels'] else 'pending_or_unresolved')
                documents.append(topic);topics.append(topic)
        local_fields=next((d.get('field_states',{}) for d in documents if d['stage']=='visual_fields'),{})
        topic_fields={key:value for item in topics for key,value in item['field_states'].items()}
        local_provenance=next((d.get('provenance',{}) for d in documents if d['stage']=='visual_fields'),{})
        return {'schema_version':VERSION,'asset_id':asset['asset_id'],'source_sha256':asset['blob_sha256'],
            'state':'ai_draft','stages':states,'labels':[l for d in documents for l in d['labels']],
            'features':[f for d in documents for f in d['features']],
            'medical_confirmation':False,'privacy_confirmation':False,'license_status':'unverified',
            'effective_values_written':False,'filename_used_as_evidence':False,
            'field_states':{**local_fields,**topic_fields},
            'field_provenance':local_provenance,
            'topic_field_provenance':{key:value for item in topics for key,value in item.get('provenance',{}).items()},
            'field_candidates':{key:value for item in topics for key,value in item.get('candidates',{}).items()},
            'normalizations':next((d.get('normalizations',[]) for d in documents if d['stage']=='visual_fields'),[]),
            'uncertainties':next((d['uncertainties'] for d in documents if d['stage']=='visual_fields'),[])}
