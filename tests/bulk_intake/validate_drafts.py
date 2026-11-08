"""Real current-batch AI projection/search/export validation; sources read only."""
import argparse
from collections import Counter
import csv
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sqlite3
import time
import httpx
from media_tag.catalog.service import encode
from media_tag.intake.inventory import load_batch
from media_tag.intake.client import IntakeClient,local_settings
from media_tag.intake.drafts import materialize
from media_tag.terminology.contracts import normalize


PROTECTED=('effective_values','tag_suggestions','privacy_reviews','index_confirmations','terminology_releases','asset_permissions','review_decisions')


def snapshot(root):
    with sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True) as c:
        result={name:sha256(encode(sorted(c.execute('SELECT * FROM '+name).fetchall())).encode()).hexdigest() for name in PROTECTED}
        result['assets']=sha256(encode(c.execute('SELECT asset_id,blob_sha256,revision,name,owner_id FROM assets ORDER BY asset_id').fetchall()).encode()).hexdigest()
        result['analysis_tasks']=c.execute('SELECT count(*) FROM router_tasks').fetchone()[0]
        return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--batch',type=Path,required=True);parser.add_argument('--local-root',type=Path,required=True);parser.add_argument('--baseline-only',action='store_true')
    args=parser.parse_args();batch,inventory,progress=load_batch(args.batch.absolute());root=args.local_root.absolute()
    baseline=batch/'ai-drafts-before.json'
    if args.baseline_only:
        if baseline.exists():raise ValueError('baseline already exists')
        baseline.write_text(encode(snapshot(root)),encoding='utf8');print('baseline saved');return
    assert baseline.exists()
    settings=local_settings(root);client=IntakeClient(settings)
    started=time.monotonic();result=materialize(batch,client);assert result['state']=='ready' and result['asset_rows']==415
    assert snapshot(root)==json.loads(baseline.read_text())
    folder=Path(result['output_dir']);records=[json.loads(line) for line in (folder/'ai-drafts.jsonl').read_text().splitlines()]
    assert len(records)==415 and len({r['asset_id'] for r in records})==415
    assert result['visual_indexed_assets']==415
    tokens=(root/'owner-token.local.txt').read_text().strip()
    with httpx.Client(base_url=settings.base_url,headers={'Authorization':'Bearer '+tokens},trust_env=False,timeout=90) as http:
        names={r.key:Path(r.key).name for r in inventory.entries}
        candidate=next((r,l) for r in records for l in r['labels'] if l['kind']=='ocr_text' and 4<=len(l['text'])<=40 and normalize(l['text']) not in normalize(names[r['source_key']]))
        record,label=candidate
        def search(payload,path='/search/query'):
            response=http.post(path,json=payload);response.raise_for_status();return response.json()
        body={'request':{'query':label['text'],'use_model':False},'include_drafts':True,'limit':50}
        found=search(body);versions=[v for g in found['items'] for v in g['versions']]
        hit=next(v for v in versions if v['asset_id']==record['asset_id'])
        assert any(r['kind']=='ai_draft' and r['draft_kind']=='ocr_text' and r['report_sha256']==label['report_sha256'] and not r['medical_evidence'] for r in hit['reasons'])
        excluded=search(body|{'include_drafts':False})
        assert record['asset_id'] not in {v['asset_id'] for g in excluded['items'] for v in g['versions']}
        public=search({'query':label['text'],'include_drafts':True,'use_model':False,'limit':50},'/v1/assets/search')
        assert any(v['asset_id']==record['asset_id'] and any(r['kind']=='ai_draft' for r in v['match_reasons']) for v in public['items'])
        visual=search({'request':{'query':'人体器官示意图','use_model':False},'include_drafts':True,'include_visual_drafts':True,'limit':5})
        assert visual['visual_drafts']['state']=='ready_uncalibrated' and visual['items']
        assert all(any(r['kind']=='ai_visual_draft' and r['calibration_state']=='not_calibrated' and not r['medical_evidence'] for r in v['reasons']) for g in visual['items'] for v in g['versions'])
        first_ids={g['root_asset_id'] for g in visual['items']};assert visual['next']
        second=search({'request':{'query':'人体器官示意图','use_model':False},'include_drafts':True,'include_visual_drafts':True,'limit':5,'cursor':visual['next']})
        assert second['items'] and first_ids.isdisjoint(g['root_asset_id'] for g in second['items'])
        # Public/Codex contract uses the same explicit visual-draft option.
        visual_public=search({'query':'人体器官示意图','include_drafts':True,'include_visual_drafts':True,'use_model':False},'/v1/assets/search')
        assert visual_public['visual_drafts']['state']=='ready_uncalibrated' and visual_public['items']
        # No authenticated draft detail through an anonymous or unshared reader.
        endpoint='/search/assets/'+record['asset_id']+'/ai-draft'
        assert http.get(endpoint,headers={'Authorization':''}).status_code==401
        reader=(root/'reader-token.local.txt')
        if reader.exists():assert http.get(endpoint,headers={'Authorization':'Bearer '+reader.read_text().strip()}).status_code==403
    rows=list(csv.DictReader(StringIO((folder/'assets.csv').read_text(encoding='utf-8-sig'),newline='')))
    assert len(rows)==415 and all(len(row)==35 for row in rows)
    assert all(row['标签状态']=='AI初稿' and row['许可状态']=='未核实' and not row['打标人'] and all(not row[field] for field in ('科室','器官/系统','疾病/症状','过程/状态')) for row in rows)
    assert sum(row['含人脸']=='有-需打码' for row in rows)==result['face_detected_assets']
    assert not any(row['含人脸']=='无' for row in rows)
    repeated=materialize(batch,client);assert repeated==result
    assert snapshot(root)==json.loads(baseline.read_text())
    report={**result,'validation_state':'passed','ocr_literal_hit_with_evidence':True,'draft_opt_out_passed':True,
        'public_api_passed':True,'visual_search_and_pagination_passed':True,'permission_checks_passed':True,
        'all_protected_records_unchanged':True,'repeat_idempotent':True,'stage_counts':dict(Counter(s+':'+v for r in records for s,v in r['stages'].items())),
        'elapsed_seconds':time.monotonic()-started}
    (batch/'ai-drafts-validation.json').write_text(encode(report),encoding='utf8')
    (batch/'ai-drafts-browser.local.json').write_text(encode({'base_url':settings.base_url,'token_file':str(root/'owner-token.local.txt'),'query':label['text'],'asset_id':record['asset_id'],
        'chrome':'C:/Program Files/Google/Chrome/Application/chrome.exe','playwright':'C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright','output_dir':str(folder)}),encoding='utf8')
    print(encode({k:report[k] for k in ('validation_state','asset_rows','ready_drafts','label_rows','ocr_labels','face_detected_assets','visual_indexed_assets','formal_columns','output_dir','elapsed_seconds')}))


if __name__=='__main__':main()
