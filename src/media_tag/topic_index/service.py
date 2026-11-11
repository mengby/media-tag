"""Ten-field precision projection; media proof remains the original report, not this cache."""
from hashlib import sha256
import json

from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import encode
from media_tag.files.service import FileService
from media_tag.models.contracts import ModelFailure
from media_tag.precision.contracts import PrecisionOptions
from media_tag.precision.projection import verified
from media_tag.router.models import StepSpec
from media_tag.storage import StorageError

VERSION='ct-topic-index-v1'


def _value(state,reason):
    return {'state':state,'values':None,'reason':reason}


class TopicIndex:
    def __init__(self,router):
        self.router=router

    @staticmethod
    def rows(connection,asset_id):
        return connection.execute('''SELECT t.*,t.rowid AS task_order,j.cancelled AS job_cancelled,
            a.envelope_json,a.result_json,a.result_digest,a.released_at
            FROM router_tasks t JOIN router_jobs j USING(job_id)
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE j.asset_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')='precision'
            ORDER BY t.rowid DESC''',(asset_id,)).fetchall()

    def _private_value(self,value,task,report,result):
        text=value.replace('\\','/').casefold()
        private=[str(self.router.workspace),self.router.workspace.as_posix(),task.output_prefix,report.provider.adapter_path,
            *(item.ref for item in result.artifacts)]
        return any(marker and marker.replace('\\','/').casefold() in text for marker in private)

    def binding(self,connection,asset,*,rows=None):
        """Metadata fence only: safe to recheck without hashing media under a writer."""
        source_rows=self.rows(connection,asset['asset_id']) if rows is None else rows
        policy=getattr(self.router.catalog,'review_policy',None)
        decisions=[tuple(row) for row in connection.execute('SELECT decision_id,decision,payload_json FROM review_decisions WHERE asset_id=? ORDER BY decision_id',(asset['asset_id'],))]
        effective=[tuple(row) for row in connection.execute('SELECT field,payload FROM effective_values WHERE asset_id=? ORDER BY field',(asset['asset_id'],))]
        gate=FileService(self.router)._gate(connection,asset)
        return sha256(encode([VERSION,asset['asset_id'],asset['revision'],asset['blob_sha256'],getattr(policy,'digest',None),gate,effective,decisions,
            [[row['task_id'],row['job_id'],row['job_cancelled'],row['state'],row['spec_json'],row['envelope_json'],
                row['result_json'],row['released_at']] for row in source_rows]]).encode()).hexdigest()

    def project(self,connection,asset,*,persist=False,rows=None,cached_documents=None,select_document=None):
        if select_document is not None and persist:
            raise ValueError('topic search selection must be read-only')
        source_rows=self.rows(connection,asset['asset_id']) if rows is None else [row for row in rows
            if json.loads(row['spec_json'])['stage']=='precision']
        source_rows=sorted(source_rows,key=lambda row:row['task_order'],reverse=True)
        if not source_rows:
            if persist:connection.execute("DELETE FROM ai_draft_documents WHERE asset_id=? AND stage='precision'",(asset['asset_id'],))
            return None,()
        humans={row[0] for row in connection.execute('SELECT field FROM effective_values WHERE asset_id=?',(asset['asset_id'],))}
        # Rejections and field effective values are version-bound, and their actual
        # payloads are included so even a repair/import cannot leave a stale cache.
        gate=FileService(self.router)._gate(connection,asset)
        basis=self.binding(connection,asset,rows=source_rows)
        claimed=set();scopes=[]
        for row in source_rows:
            spec=StepSpec.model_validate_json(row['spec_json'])
            selected=tuple(field for field in TOPIC_FIELDS if field in spec.target_fields and field not in claimed)
            if selected:
                claimed.update(selected);scopes.append((row,spec,selected))
        cached=(cached_documents.get((asset['asset_id'],'precision',basis)) if cached_documents is not None else None)
        if cached_documents is None:
            old=connection.execute("SELECT payload_json FROM ai_draft_documents WHERE asset_id=? AND stage='precision' AND basis_sha256=?",(asset['asset_id'],basis)).fetchone()
            cached=old[0] if old else None
        if cached is not None and select_document is not None:
            indexed=json.loads(cached)
            if not select_document(indexed):
                return {'schema_version':VERSION,'stage':'precision','labels':[],'features':[],
                    'field_states':{field:_value('unknown','not_selected_for_query') for field in claimed},
                    'provenance':{},'candidates':{},'state':'not_selected',
                    'claimed_fields':[field for field in TOPIC_FIELDS if field in claimed]},tuple(field for field in TOPIC_FIELDS if field in claimed)
        document={'schema_version':VERSION,'stage':'precision','source_sha256':asset['blob_sha256'],
            'labels':[],'features':[],'field_states':{},'provenance':{},'candidates':{},'producer_states':{},
            'claimed_fields':[field for field in TOPIC_FIELDS if field in claimed],
            'medical_confirmation':False,'privacy_confirmation':False,'effective_values_written':False,'state':'ready'}
        for row,spec,selected in scopes:
            state=('awaiting_file_gate' if gate else 'cancelled' if row['job_cancelled'] else row['state'])
            document['producer_states'][row['step_key']]=state
            for field in selected:
                document['field_states'][field]=_value('failed' if state in ('failed','needs_attention') else 'unknown','precision_'+state)
            if gate or row['job_cancelled'] or row['state']!='succeeded' or row['released_at'] is None or not row['result_json']:
                continue
            try:
                options=PrecisionOptions.model_validate_json(json.dumps(spec.config))
                task,result,report,ref=verified(self.router,row,asset,options.content_kind)
                sample=report.document.evidence[0]
                suggestions={item.field:item for item in result.suggestions}
                for item in report.document.fields:
                    field=item.field
                    if field not in selected:continue
                    suggestion=suggestions[field]
                    stored=connection.execute('SELECT run_id FROM tag_suggestions WHERE asset_id=? AND suggestion_id=?',(asset['asset_id'],suggestion.suggestion_id)).fetchone()
                    if stored is None:raise ValueError('precision suggestion ledger missing')
                    run_id=stored[0]
                    rejected=connection.execute("SELECT 1 FROM review_decisions WHERE asset_id=? AND decision='reject' AND json_extract(payload_json,'$.run_id')=? AND json_extract(payload_json,'$.suggestion_id')=?",(asset['asset_id'],run_id,suggestion.suggestion_id)).fetchone()
                    value=item.value
                    if rejected:
                        document['field_states'][field]=_value('unknown','human_rejected_suggestion')
                        continue
                    if any(self._private_value(text,task,report,result) for text in value.values or ()):
                        document['field_states'][field]=_value('unknown','private_value_suppressed')
                        continue
                    document['field_states'][field]={'state':value.state,'values':list(value.values) if value.values else None,
                        'reason':None if value.state=='observed' else 'precision_'+value.state}
                    if item.candidates:
                        document['candidates'][field]=[candidate for candidate in item.candidates if not self._private_value(candidate,task,report,result)]
                    provenance={'task_id':task.task_id,'lease_epoch':task.lease_epoch,'model_revision':task.versions.model_revision,
                        'preprocess_version':task.versions.preprocess_version,'prompt_version':report.prompt_version,
                        'report_sha256':ref.sha256,'content_evidence_sha256':sample.artifact.sha256,
                        'evidence_id':sample.evidence_id,'evidence_kind':sample.kind,'start_seconds':sample.start_seconds,
                        'end_seconds':sample.end_seconds,'run_id':run_id,'suggestion_id':suggestion.suggestion_id,
                        'medical_confirmation':False,'privacy_confirmation':False,'effective_values_written':False}
                    document['provenance'][field]=provenance
                    if value.state!='observed' or field in humans:continue
                    for number,text in enumerate(value.values):
                        document['labels'].append({'draft_id':suggestion.suggestion_id+'-'+str(number),'kind':'field_suggestion',
                            'field':field,'value':text,'text':text,'state':'ai_draft','confidence':None,
                            'score_kind':'generative_draft_not_probability','origin':'precision',**provenance})
                document['producer_states'][row['step_key']]='verified_precision_draft'
            except (StorageError,OSError,ValueError,KeyError,TypeError,StopIteration,ModelFailure):
                # Clear any partially adopted fields from this run; no older scope fallback.
                document['labels']=[label for label in document['labels'] if label['field'] not in selected]
                for field in selected:
                    document['field_states'][field]=_value('failed','precision_evidence_unavailable')
                    document['provenance'].pop(field,None);document['candidates'].pop(field,None)
                document['producer_states'][row['step_key']]='unavailable'
        if persist:
            connection.execute('''INSERT INTO ai_draft_documents VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id,stage) DO UPDATE SET
                source_sha256=excluded.source_sha256,basis_sha256=excluded.basis_sha256,payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
                (asset['asset_id'],'precision',asset['blob_sha256'],basis,encode(document),utc_now()))
        return document,tuple(field for field in TOPIC_FIELDS if field in claimed)
