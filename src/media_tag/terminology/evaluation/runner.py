"""Read-only HTTP comparison; never publish terms, change thresholds or approve assets."""
from hashlib import sha256
import json
from pathlib import Path
import time
from urllib.error import HTTPError,URLError
from urllib.parse import urlsplit,quote
from urllib.request import Request,build_opener,ProxyHandler,HTTPRedirectHandler
from media_tag.catalog.service import encode
from media_tag.terminology.contracts import normalize
from .models import Dataset
from .metrics import score,report


class EvaluationError(ValueError):pass


def load_dataset(path):
    raw=Path(path).read_bytes()
    if len(raw)>8*1024**2:raise EvaluationError('dataset_byte_budget')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise EvaluationError('duplicate_dataset_key')
            value[key]=item
        return value
    json.loads(raw,object_pairs_hook=unique,parse_constant=lambda value:(_ for _ in ()).throw(EvaluationError('nonfinite_dataset_number')))
    return Dataset.model_validate_json(raw),sha256(raw).hexdigest(),raw


def validate_split(dataset,calibration,calibration_sha):
    if dataset.split!='evaluation' or calibration.split!='calibration' or dataset.calibration_sha256!=calibration_sha:
        raise EvaluationError('separate_calibration_binding_required')
    if dataset.purpose!=calibration.purpose or dataset.pin!=calibration.pin or dataset.candidate_pool_sha256!=calibration.candidate_pool_sha256:
        raise EvaluationError('calibration_scope_changed')
    if dataset.criteria!=calibration.criteria or calibration.frozen_at>dataset.frozen_at:raise EvaluationError('calibration_criteria_or_chronology_changed')
    if {c.source_group for c in dataset.cases}&{c.source_group for c in calibration.cases}:
        raise EvaluationError('source_group_leakage')
    if {normalize(c.query) for c in dataset.cases}&{normalize(c.query) for c in calibration.cases}:
        raise EvaluationError('query_leakage')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):raise EvaluationError('redirect_forbidden')


class Client:
    def __init__(self,base_url,token,*,timeout=30):
        parsed=urlsplit(base_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
            raise EvaluationError('base_origin_required')
        if parsed.scheme!='https' and not (parsed.scheme=='http' and parsed.hostname in ('127.0.0.1','localhost','::1')):
            raise EvaluationError('tls_or_loopback_required')
        if not token or len(token)>4096 or any(c.isspace() for c in token):raise EvaluationError('invalid_token_file')
        self.base=base_url.rstrip('/');self.token=token;self.timeout=timeout;self.calls=0
        self.opener=build_opener(ProxyHandler({}),NoRedirect())

    def call(self,path,payload=None):
        self.calls+=1
        request=Request(self.base+path,data=encode(payload).encode() if payload is not None else None,
            headers={'Authorization':'Bearer '+self.token,'Content-Type':'application/json','Cache-Control':'no-store'})
        try:
            with self.opener.open(request,timeout=self.timeout) as response:raw=response.read(16*1024**2+1)
        except HTTPError as exc:raise EvaluationError('http_'+str(exc.code)) from None
        except TimeoutError:raise EvaluationError('transport_timeout') from None
        except URLError as exc:raise EvaluationError('transport_timeout' if isinstance(exc.reason,TimeoutError) else 'transport_unavailable') from None
        if len(raw)>16*1024**2:raise EvaluationError('response_byte_budget')
        try:return json.loads(raw)
        except (ValueError,UnicodeError):raise EvaluationError('invalid_response') from None


def pool(client):
    assets={};cursor='';seen=set()
    while True:
        value=client.call('/ui/library?limit=50&after='+quote(cursor,safe=''))
        for group in value['items']:
            for asset in group['versions']:
                if asset['file_access']=='authorized':assets[asset['asset_id']]={'asset_id':asset['asset_id'],'revision':asset['revision'],'sha256':asset['sha256']}
        if len(assets)>50000:raise EvaluationError('pool_byte_budget')
        cursor=value.get('next') or ''
        if not cursor:break
        if cursor in seen:raise EvaluationError('pool_cursor_cycle')
        seen.add(cursor)
        if len(seen)>1000:raise EvaluationError('pool_page_budget')
    records=sorted(assets.values(),key=lambda a:a['asset_id'])
    return sha256(encode(records).encode()).hexdigest(),assets


def pin_versions(versions,pin):
    return bool(versions and all(versions.get(key)==value for key,value in pin.model_dump(mode='json').items()))


def snapshot(client,dataset):
    vocabulary=client.call('/search/vocabulary')
    if not pin_versions(vocabulary.get('versions'),dataset.pin):raise EvaluationError('semantic_version_changed')
    digest,assets=pool(client)
    if digest!=dataset.candidate_pool_sha256:raise EvaluationError('candidate_pool_changed')
    concepts={c['concept_id'] for c in vocabulary['concepts']}
    for case in dataset.cases:
        if not set(case.acceptable_concepts)<=concepts:raise EvaluationError('annotation_concept_unavailable')
        if not set(case.relevant_asset_ids)<=assets.keys():raise EvaluationError('annotation_relevant_asset_not_visible')
    return {'versions':dataset.pin.model_dump(mode='json'),'candidate_pool_sha256':digest,'authorized_asset_count':len(assets)}


def compare(client,dataset,*,calibration=None,calibration_sha=None,clock=time.monotonic):
    if dataset.split=='evaluation':
        if calibration is None or calibration_sha is None:raise EvaluationError('separate_calibration_binding_required')
        validate_split(dataset,calibration,calibration_sha)
    before=snapshot(client,dataset);rows={name:[] for name in ('literal','lexicon','lexicon_bge')}
    initial={};resource={};first_model=None;devices=set()
    # Interleave the same cases, avoiding a different corpus/order for each baseline.
    for case in dataset.cases:
        for mode in rows:
            semantic=mode!='literal';started=clock();value=None;error=None
            request={'request':{'query':case.query,'context':case.context.model_dump(mode='json'),'use_model':mode=='lexicon_bge'},
                'literal_only':not semantic,'include_drafts':False,'limit':10}
            try:
                value=client.call('/search/query',request)
                if not pin_versions(value['resolution'].get('versions'),dataset.pin):raise EvaluationError('semantic_version_changed')
            except EvaluationError as exc:
                if str(exc)=='semantic_version_changed':raise
                error=str(exc)
            elapsed=clock()-started
            row=score(case,value,elapsed_seconds=elapsed,error=error,semantic=semantic)
            rows[mode].append(row)
            if mode not in initial:initial[mode]=elapsed
            if first_model is None and (value or {}).get('resolution',{}).get('model_state') in ('used','timed_out'):
                first_model={'case_id':case.case_id,'seconds':elapsed,'cold_start_proven':False}
            usage=(value or {}).get('resolution',{}).get('resource_usage',{})
            if usage.get('device') in ('cpu','cuda'):devices.add(usage['device'])
            for key in ('peak_rss_bytes','rss_bytes','peak_working_set_bytes','peak_process_bytes'):
                if type(usage.get(key)) in (int,float):resource[key]=max(resource.get(key,0),usage[key])
    after=snapshot(client,dataset)
    if before!=after:raise EvaluationError('frozen_scope_changed')
    reports={mode:report(values,dataset.criteria,semantic=mode!='literal') for mode,values in rows.items()}
    for mode,values in rows.items():
        from .metrics import aggregate
        reports[mode]['after_initial_http_latency_seconds']=aggregate(values[1:],semantic=mode!='literal')['latency_seconds']
    counts={category:sum(c.category==category for c in dataset.cases) for category in dataset.criteria.counts}
    business_eligible=bool(dataset.split=='evaluation' and dataset.purpose=='business' and dataset.independently_human_annotated
        and dataset.criteria.approved_by and all(counts[k]>=minimum for k,minimum in dataset.criteria.counts.items()))
    return {'schema_version':'d16-report-v1','dataset_id':dataset.dataset_id,'scope':before,'category_counts':counts,
        'criteria_approved':bool(dataset.criteria.approved_by),'business_eligible':business_eligible,
        'business_metric_gates_passed':bool(business_eligible and reports['lexicon_bge']['proposed_metric_gates_passed']),
        'business_acceptance':False,'business_acceptance_reason':'requires independent human review, UI load/host and acceptance evidence',
        'metrics':reports,'details':rows,'initial_http_query_seconds':initial,'first_observed_model_query':first_model,'observed_resource_usage':resource,
        'observed_model_devices':sorted(devices),'calibration_file_sha256':calibration_sha,
        'latency_scope':'serial authenticated HTTP; includes errors and cache hits, not browser first-interaction or a frozen concurrent load',
        'http_requests':getattr(client,'calls',None),'ranking_unit':'one default fixed asset version per root, as gallery chooses; first 10 roots',
        'query_oracle_selection':False,'thresholds_changed':False,'vocabulary_published':False}
