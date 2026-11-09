"""Real local draft-search timing and equivalence; source and live catalogue stay read-only."""
import argparse
import cProfile
from hashlib import sha256
import json
from pathlib import Path
import pstats
import sqlite3
import time
import httpx
import os
from concurrent.futures import ThreadPoolExecutor

from media_tag.api.config import LocalConfig
from media_tag.catalog import Catalog,Database,Principal
from media_tag.catalog.service import encode
from media_tag.contracts.templates import load_template_bundle
from media_tag.router.service import Router
from media_tag.router.runtime import cpu_stages
from media_tag.workers.probe import Toolchain
from media_tag.models.registry import ModelRegistry
from media_tag.models.runtime import model_stages
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.review.service import ReviewService
from media_tag.terminology.lexicon.service import TerminologyService
from media_tag.terminology.resolve.service import Resolver
from media_tag.search.service import SearchService
from media_tag.search.models import SearchRequest
from media_tag.search.public import AssetSearch,AssetSearchService
from media_tag.semantic.registry import SemanticRegistry
from media_tag.semantic.runtime import semantic_stage
from media_tag.intake.inventory import load_batch,hash_stream,file_identity
from media_tag.deployment.local import status


def build_search(config,snapshot):
    """Only initialize the snapshot DB; never run live worker bootstrap/recovery."""
    contract=load_template_bundle(Path(config.template_dir),expected_fingerprint=config.template_fingerprint)
    catalog=Catalog(Database(snapshot),FilesystemBackend(Path(config.object_store)),contract=contract)
    stages=cpu_stages(Toolchain(Path(config.tool_dir)))
    stages.update(model_stages(ModelRegistry(Path(config.models.model_root)),clip_device=config.models.clip_device))
    stages['visual_fields']=semantic_stage(SemanticRegistry(Path(config.semantic.model_root)))
    router=Router(catalog,Path(config.workspace),stages,options=config.options)
    reviews=ReviewService(router,config.review)
    terminology=TerminologyService(router,config.terminology)
    resolver=Resolver(terminology,config.text)
    return SearchService(resolver,reviews=reviews,options=config.search)


def signature(result):
    return sha256(encode({k:result[k] for k in ('items','index_revision','empty_reason','selected_concept_ids','intent')}).encode()).hexdigest()


def matrix(search,job,first):
    rows=[];original=search.drafts.sync;principal=Principal(actor_id='local-owner',role='editor')
    for word in (first,'red','检测到人脸','search-speed-no-match-20261109'):
        spec=SearchRequest.model_validate({'request':{'query':word,'use_model':False},'include_drafts':True,'limit':30})
        try:
            search.drafts.sync=lambda c,asset,**kw:original(c,asset,persist=False)
            started=time.perf_counter();full=search.search(spec,principal=principal);before=time.perf_counter()-started
        finally:search.drafts.sync=original
        started=time.perf_counter();fast=search.search(spec,principal=principal);after=time.perf_counter()-started
        assert signature(full)==signature(fast)
        row={'query':word,'before_seconds':before,'after_seconds':after,'speedup':before/after,
            'signature':signature(fast),'equal_items_ranking_and_evidence':True,'page_items':len(fast['items'])}
        rows.append(row);(job/'matrix.json').write_text(encode(rows),encoding='utf8');print(encode(row),flush=True)


def public_signature(result):
    return sha256(encode({k:result[k] for k in ('items','index_revision','empty_reason','selected_concept_ids','intent','result_state')}).encode()).hexdigest()


def http_matrix(root,job,config,browser):
    rows=json.loads((job/'matrix.json').read_text());measurements=[];public=[]
    snapshot=root/'backups'/(job.name+'.sqlite3');search=build_search(config,snapshot)
    token=(root/'owner-token.local.txt').read_text().strip()
    try:
        with httpx.Client(base_url=browser['base_url'],headers={'Authorization':'Bearer '+token},trust_env=False,timeout=120) as http:
            for round_number in range(3):
                for row in rows:
                    body={'request':{'query':row['query'],'use_model':False},'include_drafts':True,'limit':30}
                    started=time.perf_counter();response=http.post('/search/query',json=body);response.raise_for_status()
                    seconds=time.perf_counter()-started;assert signature(response.json())==row['signature']
                    measurements.append({'round':round_number+1,'query':row['query'],'seconds':seconds,'signature_equal':True})
            for row in rows:
                body={'query':row['query'],'use_model':False,'include_drafts':True,'limit':30}
                expected=AssetSearchService(search).query(AssetSearch.model_validate(body),principal=Principal(actor_id='local-owner',role='editor'))
                started=time.perf_counter();response=http.post('/v1/assets/search',json=body);response.raise_for_status()
                seconds=time.perf_counter()-started;assert public_signature(response.json())==public_signature(expected)
                public.append({'query':row['query'],'seconds':seconds,'signature_equal':True})
    finally:search.resolver.close()
    report={'state':'passed','personnel':measurements,'public':public,'query_classes':len(rows),'rounds':3,
        'personnel_max_seconds':max(r['seconds'] for r in measurements),'public_max_seconds':max(r['seconds'] for r in public)}
    (job/'http-matrix.json').write_text(encode(report),encoding='utf8')
    print(encode({k:v for k,v in report.items() if k not in ('personnel','public')}),flush=True)


def catalogue_stamp(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as c:
        names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name:sha256(encode(sorted(encode(tuple(r)) for r in c.execute('SELECT * FROM "'+name+'"'))).encode()).hexdigest() for name in names}


def integrity(root,job,config):
    before=catalogue_stamp(root/'backups'/(job.name+'.sqlite3'));after=catalogue_stamp(Path(config.database))
    assert before==after,{'changed_tables':[k for k in before if before[k]!=after.get(k)]}
    _,inventory,_=load_batch(Path('jobs/intake-ebe418409ddb').resolve())
    sources=FilesystemBackend(Path(inventory.source_root));entries=[r for r in inventory.entries if r.state=='ready']
    def verify(row):
        with sources.open_read(row.key) as stream:
            assert file_identity(os.fstat(stream.fileno()))==(row.size_bytes,row.mtime_ns,row.device,row.inode)
            assert hash_stream(stream)==row.sha256
        return row.size_bytes
    with ThreadPoolExecutor(max_workers=4) as pool:size=sum(pool.map(verify,entries))
    service=status(root);assert service['running'] and service.get('restart_count')==0
    with sqlite3.connect(Path(config.database).as_uri()+'?mode=ro',uri=True) as c:
        assert c.execute('SELECT count(*) FROM router_attempts WHERE released_at IS NULL').fetchone()[0]==0
    report={'state':'passed','all_catalogue_tables_unchanged':len(before),'table_fingerprints':before,
        'source_identity_and_sha_verified':len(entries),'source_bytes':size,'active_attempts':0,'service_running':True,'automatic_restart_count':0}
    (job/'integrity.json').write_text(encode(report),encoding='utf8')
    print(encode({k:v for k,v in report.items() if k!='table_fingerprints'}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True);parser.add_argument('--job',type=Path,required=True)
    parser.add_argument('--baseline',action='store_true');parser.add_argument('--profile',action='store_true')
    parser.add_argument('--local-only',action='store_true')
    parser.add_argument('--matrix',action='store_true')
    parser.add_argument('--http-matrix',action='store_true')
    parser.add_argument('--integrity',action='store_true')
    args=parser.parse_args();root=args.root.resolve();job=args.job.resolve();job.mkdir(parents=True,exist_ok=True)
    if args.baseline and (job/'before.json').exists():raise ValueError('baseline already exists; use a new job directory')
    (job/'README.md').write_text('Private local search timing, full response fingerprints and profiling; no medical approval or source modification. No credentials are copied.\n')
    config=LocalConfig.model_validate_json((root/'runtime.local.json').read_bytes())
    if args.integrity:integrity(root,job,config);return
    browser=json.loads(Path('jobs/intake-ebe418409ddb/visual-fields-browser.local.json').read_text())
    query=browser['query'];body={'request':{'query':query,'use_model':False},'include_drafts':True,'limit':30}
    if args.http_matrix:http_matrix(root,job,config,browser);return
    name='before' if args.baseline else 'after'
    if args.local_only:
        report=json.loads((job/'before.json').read_text());name='local-after'
    else:
        token=(root/'owner-token.local.txt').read_text().strip()
        with httpx.Client(base_url=browser['base_url'],headers={'Authorization':'Bearer '+token},trust_env=False,timeout=120) as http:
            started=time.perf_counter();response=http.post('/search/query',json=body);response.raise_for_status()
            result=response.json();elapsed=time.perf_counter()-started
        report={'query':query,'seconds':elapsed,'signature':signature(result),'items':len(result['items'])}
        if not args.baseline:
            before=json.loads((job/'before.json').read_text());assert report['signature']==before['signature']
            report['speedup']=before['seconds']/elapsed
        (job/(name+'.json')).write_text(encode(report),encoding='utf8')
        print(encode(report),flush=True)
    if args.profile or args.local_only:
        snapshot=root/'backups'/(job.name+'.sqlite3')
        if not snapshot.exists():
            with sqlite3.connect(Path(config.database).as_uri()+'?mode=ro',uri=True) as source,sqlite3.connect(snapshot) as dest:source.backup(dest)
        search=build_search(config,snapshot)
        try:
            profile=cProfile.Profile();started=time.perf_counter()
            local=profile.runcall(search.search,SearchRequest.model_validate(body),principal=Principal(actor_id='local-owner',role='editor'))
            assert signature(local)==report['signature']
            profile.dump_stats(job/(name+'.prof'))
            with (job/(name+'-profile.txt')).open('w') as stream:pstats.Stats(profile,stream=stream).strip_dirs().sort_stats('cumtime').print_stats(32)
            timing={'profile_seconds':time.perf_counter()-started,'signature_equal':True}
            (job/(name+'-timing.json')).write_text(encode(timing),encoding='utf8')
            print(encode(timing),flush=True)
            if args.matrix:matrix(search,job,query)
        finally:search.resolver.close()


if __name__=='__main__':main()
