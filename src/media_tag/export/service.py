"""Snapshot -> validate -> write private bundle -> atomically publish one DB pointer."""
import csv
from hashlib import sha256
from io import StringIO
import json
import os
from pathlib import Path
import time
import unicodedata
from uuid import uuid4

from media_tag.catalog import AccessDenied, NotFound, RevisionConflict
from media_tag.catalog.service import _human,encode
from media_tag.contracts.models import EffectiveValue,FieldValue,TaskEnvelope,WorkerResult
from media_tag.review.policy import ASSETS,GAPS,MEDICAL,index_status
from media_tag.storage import IntegrityError,StorageError
from media_tag.transforms.policy import review_state
from media_tag.workers.evidence import _directory

FILES=('assets.csv','gaps.csv','quality.json','manifest.json')


def safe_cell(value):
    text=str(value)
    probe=text
    while probe and (probe[0].isspace() or ord(probe[0])<32 or unicodedata.category(probe[0])=='Cf'):probe=probe[1:]
    if text and (text[0] in '\t\r\n' or probe.startswith(('=','+','-','@'))):return "'"+text,True
    return text,False


def csv_bytes(header,rows,issues,table):
    if any(safe_cell(name)[1] for name in header):raise ValueError('unsafe formal CSV header')
    stream=StringIO(newline='');writer=csv.writer(stream,lineterminator='\r\n');writer.writerow(header)
    for number,row in enumerate(rows,1):
        cells=[]
        for field in header:
            cell,escaped=safe_cell(row.get(field,''))
            if escaped:issues.append({'table':table,'row':number,'field':field,'code':'formula_escaped'})
            cells.append(cell)
        writer.writerow(cells)
    result=stream.getvalue().encode('utf-8-sig')
    parsed=list(csv.reader(StringIO(result.decode('utf-8-sig'),newline=''),strict=True))
    if tuple(parsed[0])!=header or len(parsed)!=len(rows)+1 or any(len(r)!=len(header) for r in parsed):raise ValueError('CSV contract validation failed')
    return result


class ExportService:
    def __init__(self,reviews,*,clock=time.time):
        self.reviews,self.router,self.catalog,self.database=reviews,reviews.router,reviews.catalog,reviews.database
        self.clock=clock

    def _probe(self,connection,asset):
        row=connection.execute('SELECT validation_json FROM variants WHERE asset_id=?',(asset['asset_id'],)).fetchone()
        if row:return json.loads(row[0])
        row=connection.execute("SELECT validation_json FROM uploads WHERE asset_id=? AND state='completed' ORDER BY created_at DESC LIMIT 1",(asset['asset_id'],)).fetchone()
        if row:return json.loads(row[0])
        for attempt in self.reviews.files._attempts(connection,asset['asset_id']):
            task=TaskEnvelope.model_validate_json(attempt['envelope_json']);result=WorkerResult.model_validate_json(attempt['result_json'])
            if task.stage!='probe' or task.input_refs[0].sha256!=asset['blob_sha256']:continue
            self.router._artifacts(task,result)
            ref=next((r for r in result.artifacts if r.kind=='technical_report'),None)
            if ref:
                with self.router.files.open_read(ref.ref) as stream:raw=stream.read(2*1024**2+1)
                if len(raw)>2*1024**2 or sha256(raw).hexdigest()!=ref.sha256:raise IntegrityError('technical report changed')
                return json.loads(raw)
        return {}

    def _asset_row(self,connection,asset,principal,include_ai,issues):
        identifier=asset['asset_id'];result={'素材ID':identifier,'SHA256':asset['blob_sha256'],'标签状态':'AI初稿','许可状态':'未核实',
            '下载链接':self.reviews.files._entry(identifier,asset['blob_sha256'],'content'),'入库日期':asset['created_at'][:10]}
        issues.extend({'asset_id':identifier,'field':field,'code':code} for field,code in
            [('本地路径','storage_address_not_exported'),('预览','attachment_api_not_configured'),('下载链接','relative_authenticated_entry')])
        probe=self._probe(connection,asset)
        width,height=probe.get('display_width'),probe.get('display_height')
        if width and height:result.update({'方向':'方' if width==height else '横' if width>height else '竖','分辨率':f'{width}x{height}'})
        if probe.get('kind') in ('image','gif','video'):result['素材类型']={'image':'图片','gif':'GIF','video':'视频'}[probe['kind']]
        duration=probe.get('duration_seconds')
        if duration is not None and probe.get('kind')!='image':result['时长(秒)']=f'{duration:.1f}'
        alpha=probe.get('transparency',{})
        if alpha.get('has_transparent_pixels') is True:result['透明底']='true'
        elif alpha.get('complete') and alpha.get('has_transparent_pixels') is False:result['透明底']='false'
        if probe.get('has_audio') is False:result['有原声']='false'
        elif any(t.get('silent') is False for t in probe.get('audio_tracks',[])):result['有原声']='true'
        elif probe.get('audio_tracks') and all(t.get('complete') and t.get('silent') is True for t in probe['audio_tracks']):result['有原声']='false'
        for field in ('方向','分辨率','时长(秒)','透明底','有原声'):
            if field not in result:issues.append({'asset_id':identifier,'field':field,'code':'not_applicable' if probe.get('kind')=='audio' and field in ('方向','分辨率','透明底') or probe.get('kind')=='image' and field=='时长(秒)' else 'technical_unknown'})
        values={r['field']:EffectiveValue.model_validate_json(r['payload']) for r in connection.execute('SELECT * FROM effective_values WHERE asset_id=?',(identifier,))}
        for field,effective in values.items():
            definition=self.reviews.policy.asset_fields.get(field)
            if definition is None or definition.fill_policy=='script' or field in ('素材ID','标签状态','打标人'):raise ValueError('invalid effective controlled field')
            self.reviews.policy.validate(definition,effective.value)
            record=connection.execute("SELECT payload_json FROM review_decisions WHERE asset_id=? AND field=? AND asset_revision=? AND decision!='reject'",(identifier,field,effective.revision)).fetchone()
            valid_review=bool(record and json.loads(record[0])['effective']==effective.model_dump(mode='json'))
            if field in MEDICAL and not valid_review:raise ValueError('medical effective value lacks validated human decision')
            if not valid_review:issues.append({'asset_id':identifier,'field':field,'code':'legacy_effective_provenance_unverified'})
            result.pop(field,None)
            if effective.value.state=='observed':result[field]=';'.join(effective.value.values)
            else:
                if field=='许可状态':result[field]='未核实'
                issues.append({'asset_id':identifier,'field':field,'code':effective.value.state,'reason':effective.value.reason})
        if include_ai:
            from media_tag.analysis.contracts import TOPIC_FIELDS
            from media_tag.drafts.service import DraftIndex
            draft=DraftIndex(self.router).sync(connection,asset,persist=False)
            if '含人脸' not in values:
                faces=[l for l in draft['labels'] if l.get('field')=='含人脸' and l.get('value')=='有-需打码']
                if faces:
                    result['含人脸']='有-需打码'
                    issues.append({'asset_id':identifier,'field':'含人脸','code':'unconfirmed_ai_draft',
                        'draft_ids':[l['draft_id'] for l in faces],'evidence_ids':[l['evidence_id'] for l in faces]})
            # Only current, explicitly requested nonmedical drafts, never over a human decision.
            for record in connection.execute('SELECT * FROM tag_suggestions WHERE asset_id=? ORDER BY created_at,suggestion_id',(identifier,)):
                field=json.loads(record['payload'])['field']
                if field in values or field in MEDICAL or (field in TOPIC_FIELDS and field in draft['field_states']):continue
                try:
                    suggestion,_,_=self.reviews.suggestion(connection,asset,record['run_id'],record['suggestion_id'])
                    if suggestion.value.state!='observed':
                        state=suggestion.value.state;reason=suggestion.value.reason
                        if field=='表现形式':
                            from media_tag.drafts.service import DraftIndex
                            current=DraftIndex(self.router).sync(connection,asset,persist=False)['field_states'].get(field,{})
                            state=current.get('state',state);reason=current.get('reason',reason)
                        issues.append({'asset_id':identifier,'field':field,'code':'ai_'+state,'reason':reason})
                        continue
                    rejected=connection.execute("SELECT 1 FROM review_decisions WHERE asset_id=? AND json_extract(payload_json,'$.run_id')=? AND json_extract(payload_json,'$.suggestion_id')=? AND decision='reject'",(identifier,record['run_id'],record['suggestion_id'])).fetchone()
                    definition=self.reviews.policy.asset_fields[field]
                    if rejected or definition.fill_policy not in ('ai_draft_human_sample_review',):continue
                    if field=='含人脸' and suggestion.value.values!=('有-需打码',):continue
                    self.reviews.policy.validate(definition,suggestion.value)
                    result[field]=';'.join(suggestion.value.values)
                    issues.append({'asset_id':identifier,'field':field,'code':'unconfirmed_ai_draft','run_id':record['run_id'],'suggestion_id':record['suggestion_id']})
                except (ValueError,RevisionConflict,StorageError,OSError):
                    issues.append({'asset_id':identifier,'field':field,'code':'suggestion_unavailable'})
            # Topic fields share the current-scope report projection used by search.
            # Medical suggestions belong only in the quality sidecar, never the CSV
            # effective-value columns without their separate human confirmation.
            for field in TOPIC_FIELDS:
                if field in values:continue
                labels=[label for label in draft['labels'] if label.get('field')==field]
                if labels:
                    suggested=list(dict.fromkeys(label['value'] for label in labels))
                    if field not in MEDICAL:
                        definition=self.reviews.policy.asset_fields[field]
                        self.reviews.policy.validate(definition,FieldValue(state='observed',values=tuple(suggested)))
                        result[field]=';'.join(suggested)
                    issues.append({'asset_id':identifier,'field':field,
                        'code':'unconfirmed_medical_ai_suggestion' if field in MEDICAL else 'unconfirmed_ai_draft',
                        'suggested_values':suggested,'draft_ids':[label['draft_id'] for label in labels],
                        'evidence_ids':[label['evidence_id'] for label in labels],
                        'provenance':draft.get('topic_field_provenance',{}).get(field,draft.get('field_provenance',{}))})
                else:
                    state=draft['field_states'].get(field)
                    if state:
                        issues.append({'asset_id':identifier,'field':field,'code':'ai_'+state['state'],
                            'reason':state.get('reason'),'candidates':draft.get('field_candidates',{}).get(field,[])})
        status=index_status(connection,asset,self.reviews.policy)
        if status=='confirmed' and any(issue.get('asset_id')==identifier and issue.get('code') in
                ('unconfirmed_ai_draft','unconfirmed_medical_ai_suggestion') for issue in issues):status='ai_draft'
        if status=='confirmed':
            result['标签状态']='已人工确认'
            row=connection.execute("SELECT actor_id FROM index_confirmations WHERE asset_id=? ORDER BY confirmation_id DESC LIMIT 1",(identifier,)).fetchone()
            person=self.reviews.config.people.get(row[0])
            if person:result['打标人']=person
            else:issues.append({'asset_id':identifier,'field':'打标人','code':'external_person_mapping_missing','actor_id':row[0]})
        else:issues.append({'asset_id':identifier,'field':'标签状态','code':status})
        variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(identifier,)).fetchone()
        processed=bool(variant and variant['privacy_status']=='pending' and review_state(connection,variant)=='approved')
        if result.get('含人脸')=='已处理副本' and not processed:raise ValueError('processed-copy label lacks output approval')
        if processed:result['含人脸']='已处理副本'
        # No filename-to-medical inference or fabricated usage/import history.
        for field in ('科室','器官/系统','疾病/症状','过程/状态'):
            if field not in values:issues.append({'asset_id':identifier,'field':field,'code':'medical_unconfirmed'})
        for field,definition in self.reviews.policy.asset_fields.items():
            if definition.fill_policy!='script' and field not in result and field not in MEDICAL and field!='打标人':
                issues.append({'asset_id':identifier,'field':field,'code':'human_field_unresolved'})
        return result

    def _snapshot(self,spec,principal):
        _human(principal)
        header=self.catalog.contract.table(ASSETS).header;gap_header=self.catalog.contract.table(GAPS).header
        if len(header)!=35 or len(gap_header)!=14:raise ValueError('formal two-table contract required')
        issues=[];assets=[];gaps=[];asset_refs={};gap_refs=[];alias_binding=None
        with self.database.transaction() as connection:
            if spec.include_confirmed_aliases:
                search=getattr(self.reviews,'search',None)
                if search is None:raise ValueError('terminology search is not configured')
                alias_binding=search.aliases.binding(connection)
            def authorize(identifier,revision=None,sha=None):
                row=self.catalog._authorize(connection,identifier,principal)
                if revision is not None and row['revision']!=revision or sha is not None and row['blob_sha256']!=sha:raise RevisionConflict('export selection changed')
                if self.reviews.files._gate(connection,row):raise RevisionConflict('privacy or producer gate blocks export')
                asset_refs[identifier]={'asset_id':identifier,'revision':row['revision'],'sha256':row['blob_sha256']}
                return row
            for selection in spec.assets:
                row=authorize(selection.asset_id,selection.expected_revision,selection.source_sha256)
                item=self._asset_row(connection,row,principal,spec.include_ai_drafts,issues)
                if spec.include_ai_drafts:
                    from media_tag.topic_index.service import TopicIndex
                    asset_refs[selection.asset_id]['topic_binding']=TopicIndex(self.router).binding(connection,row)
                if alias_binding:search.aliases.append(connection,row,item,issues,alias_binding)
                assets.append(item)
            for selection in spec.gaps:
                row=self.reviews._gap(connection,selection.gap_id,principal)
                if row['revision']!=selection.expected_revision:raise RevisionConflict('gap selection changed')
                values=json.loads(row['values_json']);item={'缺口ID':row['gap_id'],'出现次数':str(row['occurrences']),'首次提出日期':row['created_at'][:10]}
                for field,payload in values.items():
                    value=FieldValue.model_validate_json(encode(payload));definition=self.reviews.policy.gap_fields[field]
                    self.reviews.policy.validate(definition,value,gap=True)
                    if value.state=='observed':item[field]=';'.join(value.values)
                    else:issues.append({'gap_id':row['gap_id'],'field':field,'code':value.state})
                self.reviews._gap_links(connection,values,principal)
                for identifier in values.get('补上的素材',{}).get('values') or []:authorize(identifier)
                if row['owner_id'] in self.reviews.config.people:item['提出人']=self.reviews.config.people[row['owner_id']]
                else:issues.append({'gap_id':row['gap_id'],'field':'提出人','code':'external_person_mapping_missing','actor_id':row['owner_id']})
                gaps.append(item);gap_refs.append({'gap_id':row['gap_id'],'revision':row['revision']})
        snapshot={'schema_version':'d06-csv-snapshot-v1','template_fingerprint':self.catalog.contract.fingerprint,
            'policy_sha256':self.reviews.policy.digest,'people_mapping_sha256':sha256(encode(self.reviews.config.people).encode()).hexdigest(),
            'asset_refs':list(asset_refs.values()),'gap_refs':gap_refs,'assets':assets,'gaps':gaps,
            'asset_header':list(header),'gap_header':list(gap_header),'issues':issues,'include_ai_drafts':spec.include_ai_drafts}
        if alias_binding:snapshot['alias_binding']=alias_binding
        return snapshot

    def _current(self,connection,snapshot,principal):
        _human(principal)
        if snapshot.get('alias_binding'):
            search=getattr(self.reviews,'search',None)
            if search is None or search.aliases.binding(connection)!=snapshot['alias_binding']:raise RevisionConflict('export alias vocabulary changed')
        if snapshot['template_fingerprint']!=self.catalog.contract.fingerprint or snapshot['policy_sha256']!=self.reviews.policy.digest or snapshot['people_mapping_sha256']!=sha256(encode(self.reviews.config.people).encode()).hexdigest():raise RevisionConflict('export policy changed')
        for ref in snapshot['asset_refs']:
            row=self.catalog._authorize(connection,ref['asset_id'],principal)
            if row['revision']!=ref['revision'] or row['blob_sha256']!=ref['sha256'] or self.reviews.files._gate(connection,row):raise RevisionConflict('export snapshot is stale or privacy gated')
            if 'topic_binding' in ref:
                from media_tag.topic_index.service import TopicIndex
                if TopicIndex(self.router).binding(connection,row)!=ref['topic_binding']:raise RevisionConflict('export precision producer changed')
        for ref in snapshot['gap_refs']:
            if self.reviews._gap(connection,ref['gap_id'],principal)['revision']!=ref['revision']:raise RevisionConflict('export gap snapshot is stale')

    def create(self,spec,*,principal):
        snapshot=self._snapshot(spec,principal)
        fingerprint=sha256(encode(snapshot).encode()).hexdigest()
        with self.database.transaction(write=True) as connection:
            self._current(connection,snapshot,principal)
            request=connection.execute('SELECT * FROM csv_export_requests WHERE owner_id=? AND request_id=?',(principal.actor_id,spec.request_id)).fetchone()
            if request:
                if request['fingerprint']!=fingerprint:raise RevisionConflict('export request ID changed')
                row=connection.execute('SELECT * FROM csv_exports WHERE export_id=?',(request['export_id'],)).fetchone()
            else:
                row=connection.execute("SELECT * FROM csv_exports WHERE owner_id=? AND fingerprint=? AND state='ready' ORDER BY created_at DESC LIMIT 1",(principal.actor_id,fingerprint)).fetchone()
                if row is not None:
                    try:self._verify_bundle(row['export_id'],row['attempt'],json.loads(row['manifest_json']))
                    except (StorageError,OSError):row=None
                if row is None:
                    identifier='export-'+uuid4().hex
                    connection.execute("INSERT INTO csv_exports VALUES(?,?,?,'new',0,NULL,?,NULL,NULL,?)",(identifier,principal.actor_id,fingerprint,encode(snapshot),str(self.clock())))
                    row=connection.execute('SELECT * FROM csv_exports WHERE export_id=?',(identifier,)).fetchone()
                connection.execute('INSERT INTO csv_export_requests VALUES(?,?,?,?)',(principal.actor_id,spec.request_id,fingerprint,row['export_id']))
            identifier=row['export_id']
            if row['state']=='ready':
                manifest=json.loads(row['manifest_json'])
                self._verify_bundle(identifier,row['attempt'],manifest)
                connection.execute('INSERT INTO csv_export_heads VALUES(?,?) ON CONFLICT(owner_id) DO UPDATE SET export_id=excluded.export_id',(principal.actor_id,identifier))
                return {'export_id':identifier,'state':'ready','reused':True,'manifest':manifest}
            if row['state']=='building' and row['lease_until']>self.clock():raise RevisionConflict('export is already building')
            attempt=row['attempt']+1
            connection.execute("UPDATE csv_exports SET state='building',attempt=?,lease_until=?,error_code=NULL WHERE export_id=?",(attempt,self.clock()+300,identifier))
        started=time.monotonic()
        try:
            folder=_directory(self.router.workspace,f'jobs/{identifier}/attempt-{attempt}')
            (folder/'README.md').write_text('Private D06 immutable CSV snapshot attempt. Only DB-ready bundles are downloadable.\n',encoding='utf-8')
            (folder/'inputs.json').write_text(encode(snapshot),encoding='utf-8')
            issues=list(snapshot['issues'])
            payloads={'assets.csv':csv_bytes(tuple(snapshot['asset_header']),snapshot['assets'],issues,'assets'),
                      'gaps.csv':csv_bytes(tuple(snapshot['gap_header']),snapshot['gaps'],issues,'gaps')}
            quality={'schema_version':'d06-quality-v1','issues':issues,'asset_rows':len(snapshot['assets']),'gap_rows':len(snapshot['gaps']),
                'privacy_approval':False,'medical_final_review':False,'current_use_review':'not_checked','unknown_checkbox_encoding':'empty_not_false',
                'feishu_attachment_person_objects':False,'elapsed_seconds':time.monotonic()-started}
            payloads['quality.json']=encode(quality).encode()
            manifest={'schema_version':'d06-export-v1','export_id':identifier,'attempt':attempt,'template_fingerprint':snapshot['template_fingerprint'],
                'snapshot_sha256':fingerprint,'asset_columns':35,'gap_columns':14,'asset_rows':len(snapshot['assets']),'gap_rows':len(snapshot['gaps']),
                'files':{name:{'sha256':sha256(raw).hexdigest(),'size_bytes':len(raw)} for name,raw in payloads.items()}}
            payloads['manifest.json']=encode(manifest).encode()
            for name,raw in payloads.items():self._write(folder,name,raw)
            self._verify_bundle(identifier,attempt,manifest)
            with self.database.transaction(write=True) as connection:
                self._current(connection,snapshot,principal)
                row=connection.execute('SELECT * FROM csv_exports WHERE export_id=?',(identifier,)).fetchone()
                if row['state']!='building' or row['attempt']!=attempt or row['lease_until']<=self.clock():raise RevisionConflict('export attempt was fenced')
                connection.execute("UPDATE csv_exports SET state='ready',manifest_json=?,lease_until=NULL WHERE export_id=?",(encode(manifest),identifier))
                connection.execute('INSERT INTO csv_export_heads VALUES(?,?) ON CONFLICT(owner_id) DO UPDATE SET export_id=excluded.export_id',(principal.actor_id,identifier))
            return {'export_id':identifier,'state':'ready','reused':False,'manifest':manifest}
        except BaseException:
            with self.database.transaction(write=True) as connection:
                connection.execute("UPDATE csv_exports SET state='failed',error_code='export_not_published',lease_until=NULL WHERE export_id=? AND attempt=? AND state='building'",(identifier,attempt))
            raise

    def _write(self,folder,name,raw):
        if len(raw)>32*1024**2:raise ValueError('export file budget exceeded')
        path=folder/(name+'.tmp')
        with path.open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
        if sha256(path.read_bytes()).hexdigest()!=sha256(raw).hexdigest():raise IntegrityError('export bytes changed')
        os.replace(path,folder/name)

    def _verify_bundle(self,export_id,attempt,manifest):
        for name in FILES:
            with self.router.files.open_read(f'jobs/{export_id}/attempt-{attempt}/{name}') as stream:raw=stream.read(32*1024**2+1)
            expected=encode(manifest).encode() if name=='manifest.json' else None
            if len(raw)>32*1024**2 or (raw!=expected if expected is not None else sha256(raw).hexdigest()!=manifest['files'][name]['sha256']):
                raise IntegrityError('export bundle changed')

    def get(self,export_id,*,principal):
        _human(principal)
        with self.database.transaction() as connection:
            row=connection.execute('SELECT * FROM csv_exports WHERE export_id=?',(export_id,)).fetchone()
            if row is None:raise NotFound('export unavailable')
            if row['owner_id']!=principal.actor_id:raise AccessDenied('export owner required')
            self._current(connection,json.loads(row['snapshot_json']),principal)
            return {'export_id':export_id,'state':row['state'],'error_code':row['error_code'],'attempt':row['attempt'],
                    'manifest':json.loads(row['manifest_json']) if row['manifest_json'] else None}

    def content(self,export_id,name,*,principal):
        if name not in FILES:raise NotFound('export file unavailable')
        data=self.get(export_id,principal=principal)
        if data['state']!='ready':raise RevisionConflict('export not published')
        ref=f'jobs/{export_id}/attempt-{data["attempt"]}/{name}'
        with self.router.files.open_read(ref) as stream:raw=stream.read(32*1024**2+1)
        expected=encode(data['manifest']).encode() if name=='manifest.json' else None
        if len(raw)>32*1024**2 or (raw!=expected if expected is not None else sha256(raw).hexdigest()!=data['manifest']['files'][name]['sha256']):raise IntegrityError('published export changed')
        self.get(export_id,principal=principal)
        return raw
