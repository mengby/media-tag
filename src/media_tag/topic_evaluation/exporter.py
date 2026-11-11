"""Authorized private snapshot exporter. No model execution or catalogue mutations."""
from contextlib import contextmanager
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from typing import Annotated
from uuid import uuid4
from collections.abc import Mapping

from pydantic import Field,TypeAdapter
from media_tag.analysis.intake import INTAKE_POLICY
from media_tag.analysis.projection import TopicProjection
from media_tag.analysis.scopes import latest_topic_fields
from media_tag.catalog import AccessDenied
from media_tag.contracts.models import ArtifactRef,ContractModel,TaskEnvelope,WorkerResult,Identifier
from media_tag.files.service import FileService,FileError
from media_tag.router.service import Router
from media_tag.storage.filesystem import FilesystemBackend
from .io import EvaluationError,load,strict_json
from .models import Dataset,Export,Prediction

SUPPORTED=frozenset(('visual_fields','local_topics','precision','audio_topics'))


class FreezeOptions(ContractModel):
    max_cases: Annotated[int,Field(ge=1,le=128)]=32
    max_file_bytes: Annotated[int,Field(ge=1,le=16*1024**2)]=16*1024**2
    max_source_bytes: Annotated[int,Field(ge=1,le=64*1024**2)]=64*1024**2
    max_bundle_bytes: Annotated[int,Field(ge=1,le=256*1024**2)]=256*1024**2
    max_read_bytes: Annotated[int,Field(ge=1,le=1024*1024**2)]=512*1024**2
    max_artifacts: Annotated[int,Field(ge=1,le=2048)]=256
    timeout_seconds: Annotated[int,Field(ge=1,le=300)]=60


def _json(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def _digest(value):return sha256(_json(value)).hexdigest()


class _Budget:
    def __init__(self,options):self.options=options;self.read=0;self.error=None;self.deadline=time.monotonic()+options.timeout_seconds
    def fail(self,code):
        self.error=code
        raise EvaluationError(code)
    def check(self):
        if self.error:raise EvaluationError(self.error)
        if time.monotonic()>=self.deadline:self.fail('freeze_timeout')
    def consume(self,count):
        self.check();self.read+=count
        if self.read>self.options.max_read_bytes:self.fail('freeze_read_budget')


class _Reader:
    def __init__(self,stream,budget):self.stream,self.budget,self.count=stream,budget,0
    def fileno(self):return self.stream.fileno()
    def read(self,size=-1):
        self.budget.check()
        remaining=self.budget.options.max_file_bytes-self.count
        maximum=min(remaining+1,self.budget.options.max_read_bytes-self.budget.read+1)
        raw=self.stream.read(maximum if size is None or size<0 else min(size,maximum))
        self.count+=len(raw);self.budget.consume(len(raw))
        if self.count>self.budget.options.max_file_bytes:self.budget.fail('freeze_file_budget')
        return raw


class _Files:
    def __init__(self,backend,allowed,budget):self.backend,self.allowed,self.budget=backend,allowed,budget
    @contextmanager
    def open_read(self,key):
        if key not in self.allowed:raise EvaluationError('freeze_artifact_outside_current_scope')
        with self.backend.open_read(key) as stream:
            if os.fstat(stream.fileno()).st_size>self.budget.options.max_file_bytes:self.budget.fail('freeze_file_budget')
            yield _Reader(stream,self.budget)


class _Router:
    def __init__(self,router,files):
        self.catalog,self.database,self.workspace,self.stages=router.catalog,router.database,router.workspace,router.stages
        self.files=files
    def _artifacts(self,task,result):return Router._artifacts(self,task,result)


def _lineage(connection,asset):
    rows=[];visited=set();current=asset
    while current is not None:
        if current['asset_id'] in visited:raise EvaluationError('freeze_lineage_cycle')
        visited.add(current['asset_id']);row={'asset':dict(current)}
        if current['kind']=='derived':
            variant=connection.execute('SELECT * FROM variants WHERE asset_id=?',(current['asset_id'],)).fetchone()
            if variant is None:raise EvaluationError('freeze_lineage_unavailable')
            row['variant']=dict(variant)
            for table in ('privacy_reviews','processing_output_reviews'):
                row[table]=[dict(r) for r in connection.execute(f'SELECT * FROM {table} WHERE asset_id=? ORDER BY review_id DESC LIMIT 16',(current['asset_id'],))]
            derivation=connection.execute('SELECT * FROM processing_derivations WHERE asset_id=?',(current['asset_id'],)).fetchone()
            row['processing_derivation']=dict(derivation) if derivation else None
            plan=connection.execute('SELECT * FROM variant_plans WHERE plan_id=?',(variant['plan_id'],)).fetchone()
            row['variant_plan']=dict(plan) if plan else None
            producer=connection.execute('''SELECT t.active,t.state,t.lease_epoch,j.cancelled,a.released_at
                FROM router_tasks t JOIN router_jobs j USING(job_id) LEFT JOIN router_attempts a
                ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.task_id=?''',(variant['task_id'],)).fetchone()
            row['variant_producer']=dict(producer) if producer else None
        rows.append(row)
        if current['kind']!='derived':break
        current=connection.execute('SELECT * FROM assets WHERE asset_id=? AND deleted_at IS NULL',(current['parent_asset_id'],)).fetchone()
    return rows


@contextmanager
def _connection(database,connection=None):
    if connection is not None:yield connection
    else:
        with database.transaction() as connection:
            connection.execute('PRAGMA query_only=ON')
            yield connection


def _snapshot(router,case,asset_id,principal,*,connection=None):
    policy=getattr(router.catalog,'review_policy',None)
    if policy is None:raise EvaluationError('freeze_current_review_policy_required')
    files=FileService(router)
    with _connection(router.database,connection) as connection:
        asset=files._asset(connection,asset_id,case.source_sha256,principal)
        gate=files._gate(connection,asset)
        if gate:raise FileError(409,gate)
        blob=connection.execute('SELECT size_bytes FROM blobs WHERE sha256=?',(case.source_sha256,)).fetchone()
        locations=connection.execute('SELECT backend_id,object_key FROM storage_locations WHERE blob_sha256=? ORDER BY CASE WHEN backend_id=? THEN 0 ELSE 1 END,backend_id',
            (case.source_sha256,router.catalog.backend_id)).fetchall()
        location=next((row for row in locations if row['backend_id'] in router.catalog.backends),None)
        if blob is None or location is None:raise EvaluationError('freeze_registered_source_unavailable')
        backend=router.catalog.backends[location['backend_id']]
        if not isinstance(backend,FilesystemBackend):raise EvaluationError('freeze_local_registered_source_required')
        intent=connection.execute('''SELECT * FROM analysis_intents WHERE asset_id=? AND requested_by=? AND policy_version=?''',
            (asset_id,principal.actor_id,INTAKE_POLICY)).fetchone()
        job=None;tasks=[];winners={}
        if intent:
            if intent['source_sha256']!=case.source_sha256 or intent['owner_id']!=asset['owner_id']:
                raise EvaluationError('freeze_intent_source_changed')
            if intent['content_kind'] is not None and intent['content_kind']!=case.content_kind:
                raise EvaluationError('freeze_modality_changed')
            binding=connection.execute('SELECT * FROM router_requests WHERE owner_id=? AND request_id=?',
                (principal.actor_id,intent['intent_id'])).fetchone()
            if binding:
                job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(binding['job_id'],)).fetchone()
                if job is None or job['owner_id']!=principal.actor_id or job['asset_id']!=asset_id:
                    raise EvaluationError('freeze_job_scope_changed')
                tasks=[dict(row) for row in connection.execute('''SELECT t.task_id,t.job_id,t.step_key,t.generation,t.active,t.spec_json,
                    t.state,t.attempt,t.lease_epoch,t.error_code,a.envelope_json,a.config_json,a.result_json,a.released_at,
                    a.result_digest,a.adopted_revision FROM router_tasks t LEFT JOIN router_attempts a
                    ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch WHERE t.job_id=? ORDER BY t.rowid''',(job['job_id'],))]
                winners=latest_topic_fields(connection,job_id=job['job_id'])
        lineage=_lineage(connection,asset)
        permission=[list(row) for row in connection.execute('SELECT permission FROM asset_permissions WHERE asset_id=? AND subject_id=? ORDER BY permission',
            (asset_id,principal.actor_id))]
    policy_state={'digest':policy.digest,'taxonomy_version':policy.config.taxonomy_version,
        'taxonomy_sha256':policy.config.taxonomy_manifest_sha256,'contract':router.catalog.contract.fingerprint}
    state={'asset':dict(asset),'lineage':lineage,'permissions':permission,'intent':dict(intent) if intent else None,
           'job':dict(job) if job else None,'tasks':tasks,'winners':winners,'policy':policy_state,
           'registered_source':{'size_bytes':blob[0],'location':dict(location),'backend_root':str(backend.root)},
           'actor_id':principal.actor_id,'actor_role':principal.role,'intake_policy':INTAKE_POLICY}
    return state,_digest(state)


def _verify_registered_source(router,state,principal,budget):
    """Hash the currently selected registered source; never copy it or use a cache fallback."""
    data=state['registered_source'];size=data['size_bytes'];asset=state['asset']
    if size>budget.options.max_source_bytes:budget.fail('freeze_source_byte_budget')
    backend=router.catalog.backends.get(data['location']['backend_id'])
    if not isinstance(backend,FilesystemBackend) or str(backend.root)!=data['backend_root']:
        raise EvaluationError('freeze_registered_backend_changed')
    files=FileService(router)
    files.authorize(asset['asset_id'],asset['blob_sha256'],principal=principal,for_bytes=True)
    digest=sha256();count=0
    with backend.open_read(data['location']['object_key']) as source:
        if os.fstat(source.fileno()).st_size!=size:raise EvaluationError('freeze_registered_source_integrity')
        while chunk:=source.read(min(1024**2,size-count+1,budget.options.max_read_bytes-budget.read+1)):
            count+=len(chunk);budget.consume(len(chunk))
            if count>size:raise EvaluationError('freeze_registered_source_integrity')
            digest.update(chunk)
        if count!=size or os.fstat(source.fileno()).st_size!=size or digest.hexdigest()!=asset['blob_sha256']:
            raise EvaluationError('freeze_registered_source_integrity')
    files.authorize(asset['asset_id'],asset['blob_sha256'],principal=principal,for_bytes=True)


def _scope(state):
    if any(row['active'] and json.loads(row['spec_json'])['stage']=='temporal_topics' for row in state['tasks']):
        return None,'topic_stage_export_not_supported'
    rows=[row for row in state['tasks'] if row['active'] and row['task_id'] in set(state['winners'].values())]
    if any(json.loads(row['spec_json'])['stage'] not in SUPPORTED for row in rows):return None,'topic_stage_export_not_supported'
    if not state['intent'] or state['intent']['content_kind'] is None or not state['job']:
        return None,'current_actor_topic_projection_not_requested'
    if state['job']['cancelled']:return None,'current_topic_job_cancelled'
    return rows,None


def _references(rows):
    refs={}
    for row in rows:
        if row['state']!='succeeded' or row['released_at'] is None or not row['result_json']:continue
        task=TaskEnvelope.model_validate_json(row['envelope_json']);result=WorkerResult.model_validate_json(row['result_json'])
        for ref in (*result.artifacts,*(e.artifact for e in result.evidence),*(r for r in task.input_refs if r.kind=='technical_report')):
            if ref.ref in refs and refs[ref.ref].sha256!=ref.sha256:raise EvaluationError('freeze_reference_conflict')
            refs[ref.ref]=ref
    return refs


def _projection(router,state,proxy):
    with router.database.transaction() as connection:
        connection.execute('PRAGMA query_only=ON')
        return TopicProjection(proxy).build(connection,state['asset'],state['intent']['content_kind'],state['job'])[0]


def _qualify(proxy,state,document,run):
    used={key for field in document.fields for key in field.producer_ids}
    producers=tuple(p for p in document.producers if p.producer_id in used)
    if not producers:
        reason=('current_topic_evidence_unavailable' if any(f.next_step=='evidence' for f in document.fields) else
            'audio_topic_export_not_ready' if document.content_kind=='audio' else
            'temporal_topic_export_not_ready' if document.content_kind in ('video','animation') else 'current_topic_projection_not_ready')
        return None,{},reason
    evidence_ids={key for field in document.fields for key in field.evidence_ids}|{key for p in producers for key in p.evidence_ids}
    evidence=tuple(e for e in document.evidence if e.evidence_id in evidence_ids)
    qualified={};refs={}
    for producer in producers:
        row=next((r for r in state['tasks'] if r['task_id']==producer.task_id),None)
        if (row is None or not row['active'] or row['state']!='succeeded' or row['released_at'] is None
                or row['lease_epoch']!=producer.lease_epoch):raise EvaluationError('freeze_producer_not_current_released')
        if row['result_digest']!=sha256(row['result_json'].encode()).hexdigest():
            raise EvaluationError('freeze_result_receipt_changed')
        if producer.stage not in SUPPORTED:return None,{},'topic_stage_export_not_supported'
        if not any(pin.versions==producer.versions and pin.prompt_version==producer.prompt_version for pin in run.producer_pins):
            raise EvaluationError('freeze_unfrozen_producer_pin')
        if producer.stage=='local_topics':
            from media_tag.local_topics.projection import verified
            task,result,report,ref=verified(proxy,row,state['asset'])
        elif producer.stage in ('precision','audio_topics'):
            from media_tag.precision.projection import verified
            task,result,report,ref=verified(proxy,row,state['asset'],document.content_kind,
                layer='precision' if producer.stage=='precision' else 'local_audio')
        else:
            from media_tag.drafts.service import DraftIndex
            task=TaskEnvelope.model_validate_json(row['envelope_json']);result=WorkerResult.model_validate_json(row['result_json'])
            definition=proxy.stages['visual_fields']
            if (task.versions.model_revision!=definition.required_model_revision
                    or task.versions.preprocess_version!=definition.required_preprocess_version
                    or task.versions.rules_version!='sha256:'+proxy.catalog.contract.fingerprint):
                raise EvaluationError('freeze_legacy_visual_policy_changed')
            drafts=DraftIndex(proxy);ref,report=drafts._report(task,result);drafts._project(task,result,ref,report)
        if (producer.report!=ref or producer.versions!=task.versions or producer.stage!=task.stage
                or task.asset_id!=state['asset']['asset_id'] or task.job_id!=state['job']['job_id']
                or task.input_refs[0].sha256!=document.content_sha256 or producer.input_fingerprint!=task.input_fingerprint
                or producer.result_sha256!=sha256(row['result_json'].encode()).hexdigest()):
            raise EvaluationError('freeze_projection_producer_binding_changed')
        for item in document.fields:
            if producer.producer_id in item.producer_ids and state['winners'].get(item.field)!=task.task_id:
                raise EvaluationError('freeze_producer_scope_superseded')
            if producer.producer_id in item.producer_ids:proxy.catalog.review_policy.validate(proxy.catalog.review_policy.asset_fields[item.field],item.value)
        qualified[producer.producer_id]=result
        for artifact in (*result.artifacts,*(e.artifact for e in evidence),*(r for r in task.input_refs if r.kind=='technical_report')):
            refs[artifact.ref]=artifact
    pruned=document.model_copy(update={'evidence':evidence,'producers':producers})
    return pruned,refs,None


class _Writer:
    def __init__(self,parent,options,budget):
        self.parent=FilesystemBackend(Path(parent),writable=True);self.options,self.budget=options,budget
        self.path=self.parent._resolve('.freeze-'+uuid4().hex[:16],allow_missing=True);self.path.mkdir()
        self.backend=FilesystemBackend(self.path,writable=True);self.created=[];self.bytes=0;self.objects={}
        self.write('README.md',b'Private frozen evaluation bundle. Contains source-bound evidence/raw producer reports; never publish or serve.\n')
    def write(self,name,data):
        if len(data)>self.options.max_file_bytes:raise EvaluationError('freeze_file_budget')
        if self.bytes+len(data)>self.options.max_bundle_bytes:raise EvaluationError('freeze_bundle_budget')
        path=self.backend._resolve(name,allow_missing=True)
        with path.open('xb') as stream:
            self.created.append(name);stream.write(data)
        self.bytes+=len(data)
        self.verify(name,sha256(data).hexdigest(),len(data))
        return ArtifactRef(kind='frozen_file',ref=name,sha256=sha256(data).hexdigest())
    def verify(self,name,expected,size):
        digest=sha256();count=0
        with self.backend.open_read(name) as stream:
            source=_Reader(stream,self.budget)
            while chunk:=source.read(1024**2):digest.update(chunk);count+=len(chunk)
        if count!=size or digest.hexdigest()!=expected:raise EvaluationError('freeze_destination_integrity')
    def copy(self,files,ref):
        if ref.sha256 in self.objects:
            return ref.model_copy(update={'ref':self.objects[ref.sha256]})
        if len(self.objects)>=self.options.max_artifacts:raise EvaluationError('freeze_artifact_count_budget')
        name='a-'+ref.sha256[:24];path=self.backend._resolve(name,allow_missing=True)
        digest=sha256();count=0
        with files.open_read(ref.ref) as source,path.open('xb') as target:
            self.created.append(name)
            while chunk:=source.read(1024**2):
                count+=len(chunk)
                if self.bytes+count>self.options.max_bundle_bytes:raise EvaluationError('freeze_bundle_budget')
                digest.update(chunk);target.write(chunk)
        if digest.hexdigest()!=ref.sha256:raise EvaluationError('freeze_copied_artifact_changed')
        self.verify(name,ref.sha256,count)
        self.bytes+=count;self.objects[ref.sha256]=name
        return ref.model_copy(update={'ref':name})
    def discard(self):
        # Flat files only; never recursively delete an unverified calculated path.
        if self.path.parent!=self.parent.root or not self.path.name.startswith('.freeze-'):raise EvaluationError('freeze_cleanup_scope')
        for name in reversed(self.created):self.backend._resolve(name,allow_missing=True).unlink(missing_ok=True)
        self.path.rmdir()
    def publish(self,receipt):
        digest=_digest(receipt);final=self.parent._resolve('bundle-'+digest[:24],allow_missing=True)
        self.path.rename(final)
        return final


def freeze_catalog_export(router,dataset_path,case_assets,run_id,*,principal,private_output,split='acceptance',options=None):
    """Freeze the actor's current authorized projection into a private offline bundle."""
    options=options or FreezeOptions();budget=_Budget(options)
    if principal.role not in ('reader','editor','admin'):raise AccessDenied('human read access required')
    dataset,dataset_sha,_=load(dataset_path,Dataset,reader=lambda stream:_Reader(stream,budget),return_raw=True)
    if dataset.frozen_at>datetime.now(timezone.utc):raise EvaluationError('freeze_dataset_time_in_future')
    run=next((r for r in dataset.runs if r.run_id==run_id),None)
    if run is None or split not in ('calibration','acceptance'):raise EvaluationError('freeze_frozen_run_and_split_required')
    cases=[c for c in dataset.cases if c.split==split and (run.case_ids is None or c.case_id in run.case_ids)]
    if not isinstance(case_assets,Mapping) or not cases or len(cases)>options.max_cases or set(case_assets)!={c.case_id for c in cases}:
        raise EvaluationError('freeze_explicit_complete_run_case_mapping_required')
    for value in case_assets.values():TypeAdapter(Identifier).validate_python(value,strict=True)
    output=FilesystemBackend(Path(private_output)).root
    roots=[Path(router.workspace).resolve(),*(backend.root for backend in router.catalog.backends.values() if isinstance(backend,FilesystemBackend))]
    if any(output==root or output in root.parents or root in output.parents for root in roots):
        raise EvaluationError('freeze_destination_must_be_private_and_disjoint')
    writer=None
    try:
        writer=_Writer(output,options,budget);predictions=[];receipts=[];snapshots=[]
        for case in cases:
            budget.check();state,fence=_snapshot(router,case,case_assets[case.case_id],principal)
            _verify_registered_source(router,state,principal,budget)
            if dataset.taxonomy_version is not None and (dataset.taxonomy_version!=state['policy']['taxonomy_version'] or dataset.taxonomy_sha256!=state['policy']['taxonomy_sha256']):
                raise EvaluationError('freeze_current_taxonomy_changed')
            rows,failure=_scope(state);document=None;refs={};proxy=None
            if rows is not None:
                allowed=_references(rows)
                if len(allowed)>options.max_artifacts:raise EvaluationError('freeze_artifact_count_budget')
                proxy=_Router(router,_Files(router.files,set(allowed),budget))
                document=_projection(router,state,proxy)
                budget.check()
                original_digest=sha256(document.model_dump_json().encode()).hexdigest()
                document,refs,failure=_qualify(proxy,state,document,run)
            prediction=dict(case_id=case.case_id,source_sha256=case.source_sha256,content_kind=case.content_kind)
            receipt={'case_id':case.case_id,'asset_id':state['asset']['asset_id'],'source_sha256':case.source_sha256,
                'asset_revision':state['asset']['revision'],'metadata_fence':fence,'policy':state['policy'],
                'job_id':state['job']['job_id'] if state['job'] else None,'failure_code':failure}
            if document is None:
                prediction.update(failure_code=failure,elapsed_seconds=None,timing_scope='not_measured')
            else:
                copies={key:writer.copy(proxy.files,ref) for key,ref in refs.items()}
                def moved(ref):return copies[ref.ref]
                document=document.model_copy(update={'evidence':tuple(e.model_copy(update={'artifact':moved(e.artifact)}) for e in document.evidence),
                    'producers':tuple(p.model_copy(update={'report':moved(p.report)}) for p in document.producers)})
                document=document.model_copy(update={'analysis_id':'frozen-'+sha256(document.model_dump_json(exclude={'analysis_id'}).encode()).hexdigest()})
                name='document-'+sha256(case.case_id.encode()).hexdigest()[:16]+'.json'
                ref=writer.write(name,document.model_dump_json().encode()).model_copy(update={'kind':'topic_analysis_document'})
                prediction.update(document=ref,elapsed_seconds=sum(WorkerResult.model_validate_json(next(row['result_json'] for row in state['tasks'] if row['task_id']==p.task_id)).elapsed_seconds for p in document.producers),
                    timing_scope='sum_verified_producer_stage_seconds')
                receipt.update(source_projection_sha256=original_digest,export_document=ref.model_dump(mode='json'),
                    artifact_redirects=[{'kind':refs[key].kind,'original_artifact_sha256':refs[key].sha256,'bundle_ref':copies[key].ref} for key in sorted(refs)])
            predictions.append(Prediction(**prediction));receipts.append(receipt);snapshots.append((case,state,fence,proxy,document is not None))
        # Re-run current report qualification after long copy reads. No writer transaction.
        for case,state,fence,proxy,has_document in snapshots:
            _verify_registered_source(router,state,principal,budget)
            if has_document:
                current=_projection(router,state,proxy)
                budget.check()
                _qualify(proxy,state,current,run)
                if sha256(current.model_dump_json().encode()).hexdigest()!=next(r['source_projection_sha256'] for r in receipts if r['case_id']==case.case_id):
                    raise EvaluationError('freeze_projection_changed_during_read')
        current_dataset,current_sha,dataset_bytes=load(dataset_path,Dataset,reader=lambda stream:_Reader(stream,budget),return_raw=True)
        if current_sha!=dataset_sha:raise EvaluationError('freeze_dataset_changed_during_read')
        writer.write('dataset.json',dataset_bytes)
        exported=Export(purpose=dataset.purpose,dataset_sha256=dataset_sha,run_id=run_id,
            split_scope=split,
            taxonomy_version=dataset.taxonomy_version,taxonomy_sha256=dataset.taxonomy_sha256,
            exported_at=datetime.now(timezone.utc),predictions=tuple(predictions))
        export_ref=writer.write('export.json',exported.model_dump_json().encode())
        receipt={'schema_version':'ct-topic-catalog-freeze-v1','dataset_sha256':dataset_sha,'run_id':run_id,'split':split,
            'actor_id':principal.actor_id,'frozen_at':datetime.now(timezone.utc).isoformat(),'cases':receipts,
            'prediction_export':export_ref.model_dump(mode='json'),
            'bytes_read_before_receipt':budget.read,'bundle_bytes_before_receipt':writer.bytes,'source_reports_copied_byte_exact':True,
            'raw_source_copy_policy':'only_current_authorized_evidence_or_producer_artifacts_private_never_public',
            'calibrated':False,'automatic_enablement':False,'business_acceptance':False}
        receipt_ref=writer.write('freeze.json',_json(receipt))
        # All long reads and output writes precede this independent, short,
        # read-only metadata fence and immediate atomic private publication.
        with router.database.transaction() as connection:
            connection.execute('PRAGMA query_only=ON')
            for case,state,fence,proxy,has_document in snapshots:
                budget.check();current,current_fence=_snapshot(router,case,case_assets[case.case_id],principal,connection=connection)
                if current_fence!=fence:raise EvaluationError('freeze_metadata_changed_during_read')
            budget.check()
        final=writer.publish(receipt)
        writer=None
        return {'directory':final,'dataset_path':final/'dataset.json','export_path':final/'export.json',
            'receipt_path':final/'freeze.json','receipt_sha256':receipt_ref.sha256,'case_count':len(cases),
            'calibrated':False,'automatic_enablement':False,'business_acceptance':False}
    except BaseException:
        if writer is not None:writer.discard()
        raise
