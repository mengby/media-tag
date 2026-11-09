"""Actual local 415-image visual-field audit, without human/medical approval."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import StringIO
import csv
import json
import os
from pathlib import Path
import sqlite3
import httpx
from media_tag.catalog.service import encode
from media_tag.deployment.local import status
from media_tag.intake.inventory import load_batch,hash_stream,file_identity
from media_tag.intake.client import IntakeClient,local_settings
from media_tag.intake.drafts import materialize
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.semantic.contracts import FIELDS,FORMS,Caption
from media_tag.semantic.prompt import PROMPT_VERSION
from media_tag.semantic.registry import SemanticRegistry
from media_tag.terminology.contracts import normalize


def connect(root):
    c=sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True);c.row_factory=sqlite3.Row;return c


def baseline(root):
    with connect(root) as c:
        records={name:sha256(encode(sorted(tuple(row) for row in c.execute('SELECT * FROM '+name))).encode()).hexdigest()
            for name in ('effective_values','field_revisions','asset_permissions','review_decisions','privacy_reviews','index_confirmations','terminology_releases','library_entries')}
        records['assets']=sha256(encode([tuple(r) for r in c.execute('SELECT asset_id,blob_sha256,name,owner_id,revision FROM assets ORDER BY asset_id')]).encode()).hexdigest()
        records['predecessors']=sha256(encode([tuple(r) for r in c.execute('''SELECT t.task_id,t.spec_json,t.state,t.attempt,t.lease_epoch,a.result_digest
            FROM router_tasks t LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE json_extract(t.spec_json,'$.stage')!='visual_fields' ORDER BY t.task_id''')]).encode()).hexdigest()
        return records


def progress(root,asset_ids):
    with connect(root) as c:
        rows=c.execute('''SELECT j.asset_id,t.state,t.error_code,a.result_json FROM router_tasks t JOIN router_jobs j USING(job_id)
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.active=1 AND j.cancelled=0 AND json_extract(t.spec_json,'$.stage')='visual_fields' ''').fetchall()
        rows=[r for r in rows if r['asset_id'] in asset_ids]
        return {'states':dict(Counter(r['state'] for r in rows)),'errors':dict(Counter(r['error_code'] for r in rows if r['error_code'])),
            'total':len(rows),'succeeded':sum(r['state']=='succeeded' for r in rows)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--batch',type=Path,required=True);parser.add_argument('--local-root',type=Path,required=True)
    parser.add_argument('--baseline-only',action='store_true');parser.add_argument('--progress',action='store_true')
    args=parser.parse_args();batch,inventory,imported=load_batch(args.batch.absolute());root=args.local_root.absolute()
    asset_ids={row['asset_id'] for row in imported['rows'].values()};before=batch/'visual-fields-before.json'
    if args.baseline_only:
        if before.exists():raise ValueError('baseline already exists')
        before.write_text(encode(baseline(root)),encoding='utf8');print('baseline saved');return
    state=progress(root,asset_ids)
    if args.progress:print(encode(state));return
    assert len(asset_ids)==415 and state['total']==415 and state['succeeded']==415,state
    assert baseline(root)==json.loads(before.read_text())
    registry=SemanticRegistry(Path('cache/models/semantic').absolute());registry.verify()
    settings=local_settings(root);client=IntakeClient(settings);result=materialize(batch,client)
    assert result['state']=='ready' and result['asset_rows']==415
    folder=Path(result['output_dir']);records=[json.loads(line) for line in (folder/'ai-drafts.jsonl').read_text().splitlines()]
    assert all(r['stages']['visual_fields']=='ready' and set(r['field_states'])==set(FIELDS) for r in records)
    assert all(not r['effective_values_written'] and not r['medical_confirmation'] for r in records)
    source=FilesystemBackend(Path(inventory.source_root));token=(root/'owner-token.local.txt').read_text().strip()
    with httpx.Client(base_url=settings.base_url,headers={'Authorization':'Bearer '+token},trust_env=False,timeout=90) as http:
        def verify(row):
            previous=imported['rows'][row.key]
            with source.open_read(row.key) as stream:
                assert file_identity(os.fstat(stream.fileno()))==(row.size_bytes,row.mtime_ns,row.device,row.inode)
                assert hash_stream(stream)==row.sha256
            response=http.get('/v1/assets/'+previous['asset_id']+'/content',params={'sha256':row.sha256})
            response.raise_for_status();assert sha256(response.content).hexdigest()==row.sha256
            return len(response.content)
        with ThreadPoolExecutor(max_workers=5) as pool:download_bytes=sum(pool.map(verify,[r for r in inventory.entries if r.state=='ready']))
        candidates=[(r,l) for r in records for l in r['labels'] if l['kind']=='field_suggestion' and l['field'] in ('画面主体','中文检索词')
            and 4<=len(l['text'])<=50 and normalize(l['text']) not in normalize(r['source_key'])]
        frequencies=Counter(l['text'] for _,l in candidates);record,label=min(candidates,key=lambda x:(frequencies[x[1]['text']],len(x[1]['text'])))
        response=http.post('/search/query',json={'request':{'query':label['text'],'use_model':False},'include_drafts':True,'limit':50});response.raise_for_status()
        hit=next(v for g in response.json()['items'] for v in g['versions'] if v['asset_id']==record['asset_id'])
        assert any(reason['kind']=='ai_draft' and reason['draft_kind']=='field_suggestion' and reason['field']==label['field'] and not reason['medical_evidence'] for reason in hit['reasons'])
        public=http.post('/v1/assets/search',json={'query':label['text'],'include_drafts':True,'use_model':False,'limit':50});public.raise_for_status()
        assert any(v['asset_id']==record['asset_id'] and any(reason.get('draft_kind')=='field_suggestion' for reason in v['match_reasons']) for v in public.json()['items'])
        (batch/'visual-fields-browser.local.json').write_text(encode({'base_url':settings.base_url,'token_file':str(root/'owner-token.local.txt'),
            'query':label['text'],'asset_id':record['asset_id'],'output_dir':str(folder),'chrome':'C:/Program Files/Google/Chrome/Application/chrome.exe',
            'playwright':'C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'}),encoding='utf8')
    table=list(csv.DictReader(StringIO((folder/'assets.csv').read_text(encoding='utf-8-sig'),newline='')))
    assert len(table)==415 and all(len(r)==35 and r['标签状态']=='AI初稿' and r['许可状态']=='未核实' and not r['打标人'] for r in table)
    assert all(not r[field] for r in table for field in ('科室','器官/系统','疾病/症状','过程/状态'))
    assert all(not r['表现形式'] or r['表现形式'] in FORMS for r in table)
    assert baseline(root)==json.loads(before.read_text())
    with connect(root) as c:
        reports=c.execute('''SELECT a.result_json FROM router_attempts a JOIN router_tasks t USING(task_id) JOIN router_jobs j USING(job_id)
            WHERE t.active=1 AND a.lease_epoch=t.lease_epoch AND json_extract(t.spec_json,'$.stage')='visual_fields' AND t.state='succeeded'
            AND j.asset_id IN ('''+','.join('?' for _ in asset_ids)+')',tuple(asset_ids)).fetchall()
        timings=[];memory=[];policies=Counter();projections=Counter();cache_hits=0
        for row in reports:
            worker=json.loads(row[0]);ref=next(r for r in worker['artifacts'] if r['kind']=='visual_fields_report')
            raw=(root/'workspace'/ref['ref']).read_bytes();assert sha256(raw).hexdigest()==ref['sha256'];report=json.loads(raw)
            assert report['model_revision']==registry.revision and report['prompt_version']==PROMPT_VERSION
            Caption.model_validate_json(json.dumps(report['caption']))
            timings.append(report.get('amortized_inference_seconds',report['elapsed_seconds']));memory.append(report['peak_gpu_allocated_bytes'])
            policies[report.get('processor_budget_policy','early_trial_legacy')]+=1
            projections[report.get('patch_projection','early_trial_original_conv3d')]+=1
            cache_hits+=bool(report.get('cache_reused'))
            if report.get('processor_budget_policy')=='size-v2':assert all(v<=512**2 for v in report['processed_pixels'])
    stable=status(root)
    report={**result,'validation_state':'passed','source_unchanged':415,'download_bytes_verified':download_bytes,
        'old_successful_stages_unchanged':True,'human_permissions_and_approvals_unchanged':True,
        'semantic_search_with_evidence':True,'public_api_passed':True,'model_revision':registry.revision,'prompt_version':PROMPT_VERSION,
        'field_state_counts':{field:dict(Counter(r['field_states'][field]['state'] for r in records)) for field in FIELDS},
        'processor_policy_counts':dict(policies),'projection_counts':dict(projections),'cached_result_count':cache_hits,
        'inference_seconds_sum':sum(timings),'peak_gpu_allocated_bytes':max(memory),'service_running':stable['running'],
        'supervisor_generation':stable['generation'],'supervisor_restart_count':stable.get('restart_count')}
    (batch/'visual-fields-validation.json').write_text(encode(report),encoding='utf8')
    print(encode({k:report[k] for k in ('validation_state','asset_rows','field_drafts','output_dir','inference_seconds_sum','peak_gpu_allocated_bytes')}))


if __name__=='__main__':main()
