"""Read-only post-import audit of a real batch; no approvals or source writes."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
import httpx
from media_tag.catalog.service import encode
from media_tag.deployment.local import read_json,status
from media_tag.intake.inventory import load_batch,hash_stream,file_identity
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import Toolchain,ProbeOptions,probe_media
from media_tag.workers.evidence import EvidenceOptions,extract_frames,_directory


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--batch',type=Path,required=True);parser.add_argument('--local-root',type=Path,required=True)
    args=parser.parse_args();batch,inventory,progress=load_batch(args.batch.absolute());root=args.local_root.absolute()
    assert progress['state']=='imported'
    token=(root/'owner-token.local.txt').read_text(encoding='utf8').strip();origin=progress['service_origin']
    before=read_json(batch/'before-import.json');source=FilesystemBackend(Path(inventory.source_root))
    with httpx.Client(base_url=origin,headers={'Authorization':'Bearer '+token},trust_env=False,timeout=45) as client:
        def verify(row):
            item=progress['rows'][row.key];assert item['state']=='imported'
            with source.open_read(row.key) as stream:
                assert file_identity(os.fstat(stream.fileno()))==(row.size_bytes,row.mtime_ns,row.device,row.inode)
                assert hash_stream(stream)==row.sha256
            detail=client.get('/v1/assets/'+item['asset_id']);detail.raise_for_status();detail=detail.json()
            assert detail['sha256']==row.sha256 and detail['size_bytes']==row.size_bytes
            digest=sha256();size=0
            with client.stream('GET',f"/v1/assets/{item['asset_id']}/content?sha256={row.sha256}") as response:
                response.raise_for_status();assert response.headers['etag']=='"'+row.sha256+'"'
                for chunk in response.iter_bytes():digest.update(chunk);size+=len(chunk)
            assert digest.hexdigest()==row.sha256 and size==row.size_bytes
            member=client.get('/ui/library/'+item['asset_id']);member.raise_for_status();assert member.json()['active']
            return {'asset_id':item['asset_id'],'source_key':row.key,'sha256':row.sha256,'bytes':size,'source_unchanged':True,'fixed_download_verified':True}
        with ThreadPoolExecutor(max_workers=4) as pool:verified=list(pool.map(verify,[r for r in inventory.entries if r.state=='ready']))
        all_ids=set();cursor=''
        while True:
            response=client.get('/ui/library',params={'limit':50,'after':cursor});response.raise_for_status();page=response.json()
            all_ids.update(v['asset_id'] for group in page['items'] for v in group['versions'])
            cursor=page['next']
            if not cursor:break
        assert {r['asset_id'] for r in verified}<=all_ids
        sample=next(r for r in inventory.entries if r.state=='ready')
        search=client.post('/search/query',json={'request':{'query':Path(sample.key).name},'literal_only':True,'include_drafts':True})
        search.raise_for_status();assert progress['rows'][sample.key]['asset_id'] in {v['asset_id'] for g in search.json()['items'] for v in g['versions']}
    asset_ids=[v['asset_id'] for v in verified];marks=','.join('?' for _ in asset_ids)
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=10)) as db:
        for aid,digest,revision in before['assets']:
            assert db.execute('SELECT blob_sha256,revision FROM assets WHERE asset_id=?',(aid,)).fetchone()==(digest,revision)
        for table in ('privacy_reviews','index_confirmations','terminology_releases','effective_values','tag_suggestions'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==before['counts'][table]
        stages=db.execute(f"SELECT json_extract(t.spec_json,'$.stage'),t.state,count(*) FROM router_tasks t JOIN router_jobs j USING(job_id) WHERE j.asset_id IN ({marks}) GROUP BY 1,2",asset_ids).fetchall()
        jobs=db.execute(f'SELECT asset_id,count(*) FROM router_jobs WHERE asset_id IN ({marks}) GROUP BY asset_id',asset_ids).fetchall()
        assert len(jobs)==len(asset_ids) and all(count==1 for _,count in jobs)
        probe_limits={r[0] for r in db.execute(f"SELECT json_extract(t.spec_json,'$.config.max_pixels') FROM router_tasks t JOIN router_jobs j USING(job_id) WHERE j.asset_id IN ({marks}) AND json_extract(t.spec_json,'$.stage')='probe'",asset_ids)}
        assert probe_limits=={32*1024**2}
    # Exercise actual high-resolution evidence without advancing/reordering the live queue.
    tools=Toolchain(Path('cache/media-tools/ffmpeg-9.0.2/bin').resolve());large=[]
    preflight=read_json(batch/'image-preflight.json')
    for index,item in enumerate(preflight['issues']):
        assert item['reason']=='pixel_budget'
        row=next(r for r in inventory.entries if r.key==item['key'])
        output=_directory(batch,f'checks/high-resolution-{index}')
        with source.open_verified(row.key,row.sha256,row.size_bytes) as stream:
            info=probe_media(Path(stream.name),tools,ProbeOptions(max_pixels=32*1024**2)).as_dict()
            report=extract_frames(Path(stream.name),info,output,batch,tools,EvidenceOptions(),row.sha256)
        assert report['status']=='succeeded' and report['frames']
        for frame in report['frames']:
            assert frame['source_sha256']==row.sha256
            artifact=frame['artifact'];assert sha256((batch/artifact['ref']).read_bytes()).hexdigest()==artifact['sha256']
        large.append({'source_key':row.key,'pixels':item['pixels'],'probe_passed':True,'evidence_extracted':True})
    result={'state':'passed','batch_id':inventory.batch_id,'python':sys.executable,'verified_sources':len(verified),
        'verified_download_bytes':sum(r['bytes'] for r in verified),'visible_in_library':True,'literal_search_passed':True,
        'preexisting_records_unchanged':True,'approval_and_effective_value_counts_unchanged':True,'one_analysis_job_per_source':True,
        'probe_max_pixels':32*1024**2,'high_resolution_checks':large,'analysis_snapshot':stages,
        'display_suffix_corrected':sum(progress['rows'][r.key].get('upload_name')!=Path(r.key).name for r in inventory.entries),
        'review_required_for_import':False,'medical_confirmation_written':False,'service_running':status(root)['state']=='running',
        'full_ai_tagging_finished':False,'license_status':'unverified','source_results':verified}
    (batch/'validation.json').write_text(encode(result),encoding='utf8')
    print(encode({k:result[k] for k in ('state','verified_sources','verified_download_bytes','visible_in_library','one_analysis_job_per_source','display_suffix_corrected')}))


if __name__=='__main__':main()
