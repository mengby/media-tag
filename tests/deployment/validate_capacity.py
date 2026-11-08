"""Bounded local D07 soak, real UI concurrency and isolated synthetic ingestion.

Production is read-only except short-lived login tickets. Never stops its supervisor.
The isolated instance is stopped normally; no restart/logout/adjacent-project access.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from uuid import uuid4
import httpx
import psutil
from PIL import Image,ImageDraw
from media_tag.catalog.service import encode
from media_tag.deployment.local import initialize,status,stop,read_json
from media_tag.deployment.capacity import directory_usage,request_summary,browser_summary,capacity_projection
from media_tag.web.local_login import TicketStore,LocalBrowserLogin

PROJECT=Path(__file__).resolve().parents[2]
PLAYWRIGHT='C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'
CHROME='C:/Program Files/Google/Chrome/Application/chrome.exe'


def save(p,value):p.write_text(encode(value),encoding='utf8')


def db(root,sql,params=()):
    with closing(sqlite3.connect((root/'catalog.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=10)) as c:
        return c.execute(sql,params).fetchall()


def mkdir(path,description):
    path.mkdir();(path/'README.md').write_text(description+'\n',encoding='utf8')


def run_tool(batch,args,timeout=120):
    name='tool-'+uuid4().hex[:8]+'.log'
    with (batch/name).open('w',encoding='utf8') as stream:
        result=subprocess.run(list(map(str,args)),stdout=stream,stderr=subprocess.STDOUT,timeout=timeout,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError('tool_failed:'+name)


def wait_for(predicate,seconds=90,*,abort=None):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if abort is not None and abort.is_set():raise RuntimeError('diagnostic_monitor_aborted')
        value=predicate()
        if value:return value
        time.sleep(.1)
    raise RuntimeError('validation_condition_timeout')


def retime_ready(values):
    if any(v['job']['state'] in ('failed','needs_attention','cancelled') for v in values):
        raise RuntimeError('retime_terminal_failure')
    return values if all(v['asset_id'] and v['job']['state']=='succeeded' for v in values) else None


def verify_pipeline(batch,runtime):
    """Read actual terminal model records and downloaded derived versions."""
    tasks=db(runtime,"SELECT json_extract(spec_json,'$.stage'),state,count(*) FROM router_tasks GROUP BY 1,2")
    clips=db(runtime,"SELECT a.result_json FROM router_attempts a JOIN router_tasks t USING(task_id,lease_epoch) WHERE json_extract(t.spec_json,'$.stage')='clip'")
    assert len(clips)==30
    for (raw,) in clips:
        record=json.loads(raw)
        assert record['status']=='partial' and {e['code'] for e in record['errors']}=={'unpublished_vocabulary'}
    assert db(runtime,'SELECT count(*) FROM router_holds')[0][0]==0
    assert db(runtime,'SELECT count(*) FROM router_attempts WHERE released_at IS NULL')[0][0]==0
    rejected=db(runtime,"SELECT state,error_code FROM router_tasks WHERE json_extract(spec_json,'$.stage')!='clip' AND state IN ('failed','needs_attention','cancelled')")
    expected=4 if read_json(batch/'profile.json')['reused_ingest_batch'] else 0
    assert len(rejected)==expected and all(state=='needs_attention' and code=='duration_budget' for state,code in rejected)
    counts={table:db(runtime,'SELECT count(*) FROM '+table)[0][0] for table in ('terminology_releases','privacy_reviews','index_confirmations')}
    assert all(count==0 for count in counts.values())
    instance=read_json(runtime/'instance.json');owner=(runtime/'owner-token.local.txt').read_text().strip();downloads=[]
    with httpx.Client(base_url=f"http://127.0.0.1:{instance['port']}",headers={'Authorization':'Bearer '+owner},trust_env=False,timeout=45) as client:
        for record in read_json(batch/'retime-results.json',limit=16*1024**2):
            detail=client.get('/v1/assets/'+record['asset_id']);detail.raise_for_status();detail=detail.json()
            manifest=client.get(f"/v1/assets/{record['asset_id']}/download-manifest?sha256={detail['sha256']}");manifest.raise_for_status()
            body=client.get(manifest.json()['content']);body.raise_for_status()
            assert sha256(body.content).hexdigest()==detail['sha256']
            downloads.append({'asset_id':record['asset_id'],'sha256':detail['sha256'],'bytes':len(body.content),'fixed_download_hash_verified':True})
    value={'state':'passed','tasks':tasks,'clip_partial_exact_reason_verified':30,'active_attempts':0,'resource_holds':0,
           'approval_and_release_counts':counts,'derived_downloads':downloads,'prior_duration_budget_rejections':len(rejected)}
    save(batch/'pipeline-postchecks.json',value);return value


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds',type=int,default=900)
    parser.add_argument('--reuse-ingest',type=Path,help='reuse the verified immutable inputs/uploads from a prior local batch')
    args=parser.parse_args()
    if not 900<=args.seconds<=3600:raise ValueError('diagnostic soak must be 900..3600 seconds')
    if Path(sys.prefix).resolve()!=PROJECT/'.venv':raise ValueError('use project .venv')
    production=PROJECT/'data/local';initial=status(production)
    assert initial['running'] and initial['state']=='running'
    assert psutil.virtual_memory().available>6*1024**3 and shutil.disk_usage(PROJECT).free>8*1024**3
    identity=uuid4().hex[:8];batch=PROJECT/'jobs'/('d07-capacity-'+identity)
    mkdir(batch,'Private bounded D07 capacity/soak/load evidence. Synthetic media; no medical, privacy, license or semantic quality approval.')
    prior=args.reuse_ingest.resolve() if args.reuse_ingest else None
    if prior and (prior.parent!=PROJECT/'jobs' or not re.fullmatch(r'd07-capacity-[0-9a-f]{8}',prior.name)):
        raise ValueError('reuse must reference an owned capacity batch')
    runtime=PROJECT/'data'/('capacity-validation-'+(prior.name.removeprefix('d07-capacity-') if prior else identity))
    if prior and status(runtime)['running']:raise ValueError('reused validation instance is already running')
    # Explicit per-request budgets within VideoOptions' supported limits. The
    # production/default 60-second/1800-frame/1G-pixel limits are unchanged.
    retime_options={'max_seconds':100.0,'max_frames':2300,'max_total_pixels':2_200_000_000}
    save(batch/'profile.json',{'seconds':args.seconds,'browser_sessions':5,'warm_queries_per_phase':200,
        'source_count':30,'video_seconds':90,'video_size':'1280x720','retime_options':retime_options,
        'reference_p95_ms':2000,'reference_error_rate':.01,'formal_scale_acceptance':False,
        'reused_ingest_batch':str(prior) if prior else None})
    save(batch/'state.json',{'state':'running','phase':'preflight','seconds':args.seconds,'started_at':time.time()})
    before_assets=db(production,'SELECT asset_id,blob_sha256,revision FROM assets ORDER BY asset_id')
    config_hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in production.glob('*.local.json')}
    # Capacity metadata only: do not read source contents or traverse linked roots.
    buckets=['data/local','jobs','cache','.venv']
    inventory={key:directory_usage(PROJECT/key) for key in buckets}
    disks={drive:dict(zip(('total','used','free'),shutil.disk_usage(drive))) for drive in ('C:/','D:/','E:/') if Path(drive).exists()}
    save(batch/'capacity-before.json',{'volumes':disks,'buckets':inventory,'logical_bytes_not_allocated_clusters':True})
    token=(production/'reader-token.local.txt').read_text(encoding='utf8').strip()
    origin=initial['url'].removesuffix('/ui/')
    samples=[];probe_rows=[];monitor_errors=[];activity=[];shutdown=threading.Event();children=[];private=None
    start=time.monotonic();started_at=time.time()
    def monitor():
        with httpx.Client(base_url=origin,headers={'Authorization':'Bearer '+token},trust_env=False,timeout=10) as client:
            while not shutdown.is_set():
                now=time.time();sample={'at':now,'elapsed_seconds':time.monotonic()-start,'ram_available':psutil.virtual_memory().available,'ram_percent':psutil.virtual_memory().percent}
                try:
                    current=status(production)
                    assert current['generation']==initial['generation'] and current['restart_count']==initial['restart_count'] and current['running'] and current['state']=='running'
                    roots=[initial['supervisor_pid']]
                    if private and private.poll() is None:roots.append(private.pid)
                    processes={}
                    for pid in roots:
                        for proc in [psutil.Process(pid),*psutil.Process(pid).children(recursive=True)]:
                            try:processes[proc.pid]={'pid':proc.pid,'created':proc.create_time(),'rss':proc.memory_info().rss,'cpu_seconds':sum(proc.cpu_times()[:2])}
                            except (psutil.NoSuchProcess,psutil.AccessDenied):pass
                    sample['service_processes']=list(processes.values());sample['service_rss_sum']=sum(p['rss'] for p in processes.values())
                    sample['disk_free']=shutil.disk_usage(PROJECT).free
                    if len(samples)%3==0 and shutil.which('nvidia-smi'):
                        gpu=subprocess.run(['nvidia-smi','--query-gpu=memory.used,utilization.gpu,temperature.gpu','--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=5,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                        if gpu.returncode==0:sample['host_gpu']=gpu.stdout.strip()
                    if runtime.joinpath('catalog.sqlite3').exists():
                        active=db(runtime,"SELECT task_id,json_extract(spec_json,'$.stage') FROM router_tasks WHERE state IN ('leased','running')")
                        sample['active_tasks']=active;activity.append({'at':now,'tasks':active})
                    if sample['ram_available']<2*1024**3 or sample['disk_free']<4*1024**3:raise RuntimeError('resource_reserve_reached')
                except Exception as exc:monitor_errors.append(type(exc).__name__+':'+str(exc)[:120]);shutdown.set()
                samples.append(sample)
                def probe(index):
                    begun=time.perf_counter();code=0;timeout=False
                    try:
                        response=client.get('/v1/capabilities' if index%2==0 else '/ui/library?limit=10');code=response.status_code
                    except httpx.TimeoutException:timeout=True
                    except httpx.HTTPError:pass
                    return {'elapsed_ms':(time.perf_counter()-begun)*1000,'status':code,'ok':code==200,'timeout':timeout,'at':time.time()}
                with ThreadPoolExecutor(max_workers=5) as pool:probe_rows.extend(pool.map(probe,range(5)))
                if any(not row['ok'] for row in probe_rows[-5:]):monitor_errors.append('production_probe_failed');shutdown.set()
                save(batch/'heartbeat.json',{'elapsed_seconds':time.monotonic()-start,'samples':len(samples),'probe':request_summary(probe_rows),'errors':monitor_errors})
                shutdown.wait(5)
    thread=threading.Thread(target=monitor,daemon=True);thread.start()
    browser_jobs=[]
    def browser(name,url,query,*,token_file=None,tickets=None,hold=False):
        context={'url':url,'query':query,'per_session':40,'playwright':PLAYWRIGHT,'chrome':CHROME,'result':str(batch/(name+'.json'))}
        if token_file:context['token_file']=str(token_file)
        if tickets:context['tickets']=tickets
        if hold:context['hold_until']=str(batch/'browser-stop.json')
        path=(runtime if token_file else production/'browser-login')/('capacity-'+identity+'-'+name+'.local.json')
        save(path,context)
        stream=(batch/(name+'.stderr.log')).open('w',encoding='utf8')
        process=subprocess.Popen(['node',str(PROJECT/'tests/deployment/capacity-browser.mjs'),str(path)],stdout=subprocess.DEVNULL,stderr=stream,creationflags=subprocess.CREATE_NO_WINDOW)
        stream.close();browser_jobs.append((name,process,path));return process
    result={'state':'failed','batch':str(batch),'scope':'bounded same-machine engineering diagnostic; not formal 1000-assets/2000-mappings semantic acceptance'}
    try:
        live_config=read_json(production/'runtime.local.json')
        store=TicketStore(LocalBrowserLogin.model_validate(live_config['local_browser_login']))
        tickets=[store.issue(sha256(token.encode()).hexdigest()) for _ in range(5)]
        public_browser=browser('production-browser',origin,'西柚',tickets=tickets,hold=True)
        if prior:port=read_json(runtime/'instance.json')['port']
        else:
            with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        initialize(runtime,PROJECT,port=port,vision=True)
        private_origin=f'http://127.0.0.1:{port}'
        log=(batch/'isolated-supervisor.log').open('w',encoding='utf8')
        private=subprocess.Popen([sys.executable,'-m','media_tag','local-run','--root',str(runtime)],cwd=PROJECT,stdout=subprocess.DEVNULL,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        log.close();wait_for(lambda:status(runtime)['state']=='running',abort=shutdown)
        assert not shutdown.is_set(),'monitor failed during setup'
        synthetic=(prior or batch)/'inputs'
        if not prior:mkdir(synthetic,'Synthetic engineering shapes and generated test patterns only; immutable after generation.')
        tools=PROJECT/'cache/media-tools/ffmpeg-9.0.2/bin';inputs=[]
        for n in range(24):
            path=synthetic/f'D07-load-{n:02d}.png'
            if not prior:
                image=Image.new('RGB',(640,360),(20+n*7,80,130));draw=ImageDraw.Draw(image)
                draw.rectangle((30+n,40,220+n,240),fill='orange');draw.text((270,160),f'D07 LOAD {n}',fill='white');image.save(path)
            inputs.append(path)
        for n in range(6):
            path=synthetic/f'D07-load-video-{n}.mp4'
            if not prior:
                run_tool(batch,[tools/'ffmpeg.exe','-v','error','-f','lavfi','-i',f'testsrc2=size={"1280x720" if n==0 else "320x180"}:rate=25',
                    '-t','90' if n==0 else '3','-an','-c:v','libx264','-preset','veryfast','-threads','2','-pix_fmt','yuv420p',path])
            inputs.append(path)
        source_hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in inputs};save(batch/'input-hashes.json',source_hashes)
        if prior:assert source_hashes==read_json(prior/'input-hashes.json'),'reused source input changed'
        owner=(runtime/'owner-token.local.txt').read_text().strip();upload_rows=[]
        def upload(path):
            begun=time.perf_counter();payload=path.read_bytes();digest=source_hashes[path.name]
            with httpx.Client(base_url=private_origin,headers={'Authorization':'Bearer '+owner},trust_env=False,timeout=45) as client:
                def call(method,path,**kwargs):
                    response=client.request(method,path,**kwargs)
                    if response.status_code!=200:raise RuntimeError('isolated_upload_http_'+str(response.status_code))
                    return response.json()
                data=call('POST','/uploads',json={'request_id':identity+'-'+path.stem,'filename':path.name,'size_bytes':len(payload),'sha256':digest})
                while data['offset']<len(payload):
                    offset=data['offset'];chunk=payload[offset:offset+data['chunk_bytes']]
                    data=call('PUT',f"/uploads/{data['upload_id']}/chunks?offset={offset}",content=chunk,headers={'X-Chunk-SHA256':sha256(chunk).hexdigest()})
                receipt=call('POST',f"/uploads/{data['upload_id']}/complete")
                detail=call('GET','/v1/assets/'+receipt['asset_id'])
                call('POST','/ui/library/'+receipt['asset_id'],json={'request_id':identity+'-share-'+path.stem,'expected_revision':detail['revision'],
                     'output_sha256':digest,'active':True,'recipients':['local-codex-reader']})
            return {'asset_id':receipt['asset_id'],'name':path.name,'sha256':digest,'bytes':len(payload),'ok':True,'elapsed_ms':(time.perf_counter()-begun)*1000}
        if prior:upload_rows=read_json(prior/'uploads.json')
        else:
            with ThreadPoolExecutor(max_workers=5) as pool:upload_rows=list(pool.map(upload,inputs))
        save(batch/'uploads.json',upload_rows)
        assert len(upload_rows)==30 and {r['name']:r['sha256'] for r in upload_rows}==source_hashes
        def extracted():
            rows=db(runtime,"SELECT json_extract(spec_json,'$.stage'),state,error_code FROM router_tasks WHERE json_extract(spec_json,'$.stage') IN ('probe','evidence','ocr','face','clip')")
            if any(state=='failed' for _,state,_ in rows):raise RuntimeError('isolated_worker_task_failed')
            return rows if rows and all(state=='succeeded' or stage=='clip' and state=='needs_attention' for stage,state,_ in rows) else None
        tasks=wait_for(extracted,600,abort=shutdown);save(batch/'queue-after-ingest.json',tasks)
        with httpx.Client(base_url=private_origin,headers={'Authorization':'Bearer '+owner},trust_env=False,timeout=30) as client:
            for row in upload_rows:
                detail=client.get('/v1/assets/'+row['asset_id']);detail.raise_for_status()
                assert detail.json()['technical']['report_state']=='ready'
                response=client.get(f"/v1/assets/{row['asset_id']}/content?sha256={row['sha256']}");response.raise_for_status()
                assert sha256(response.content).hexdigest()==row['sha256']
            idle=browser('isolated-idle',private_origin,'D07',token_file=runtime/'reader-token.local.txt');idle.wait(timeout=120);assert idle.returncode==0
            video=next(row for row in upload_rows if row['name']=='D07-load-video-0.mp4')
            detail=client.get('/v1/assets/'+video['asset_id']).json();retimes=[]
            for n,speed in enumerate((.5,.75,1.25,2)):
                response=client.post('/retime/jobs',json={'request_id':identity+'-retime-'+str(n),'parent_asset_id':video['asset_id'],'expected_revision':detail['revision'],'speed':speed,'audio':'drop','options':retime_options})
                response.raise_for_status();retimes.append(response.json()['job_id'])
            wait_for(lambda:db(runtime,"SELECT task_id FROM router_tasks WHERE state IN ('leased','running') AND json_extract(spec_json,'$.stage')='retime_render'"),30,abort=shutdown)
            busy_start=time.time();busy=browser('isolated-busy',private_origin,'D07',token_file=runtime/'reader-token.local.txt');busy.wait(timeout=150);assert busy.returncode==0
            def retimed():
                values=[client.get('/retime/jobs/'+job).json() for job in retimes]
                return retime_ready(values)
            outputs=wait_for(retimed,600,abort=shutdown)
            save(batch/'retime-timing-reports.json',outputs)
            save(batch/'retime-results.json',[{k:v for k,v in output.items() if k!='report'} for output in outputs])
        while time.monotonic()-start<args.seconds:
            assert not shutdown.is_set(),'production monitor failed'
            assert private.poll() is None and public_browser.poll() is None
            save(batch/'state.json',{'state':'running','phase':'soak','elapsed_seconds':time.monotonic()-start,'seconds':args.seconds})
            time.sleep(5)
        assert not shutdown.is_set()
        save(batch/'browser-stop.json',{'stop':True});public_browser.wait(timeout=20);assert public_browser.returncode==0
        shutdown.set();thread.join(20)
        assert not monitor_errors and all(row['ok'] for row in probe_rows)
        assert all(sha256(p.read_bytes()).hexdigest()==source_hashes[p.name] for p in inputs)
        assert before_assets==db(production,'SELECT asset_id,blob_sha256,revision FROM assets ORDER BY asset_id')
        assert config_hashes=={p.name:sha256(p.read_bytes()).hexdigest() for p in production.glob('*.local.json')}
        assert status(production)['generation']==initial['generation']
        for _,proc,_ in browser_jobs:assert proc.returncode==0
        browser_reports={name:read_json(batch/(name+'.json'),limit=4*1024**2) for name,_,_ in browser_jobs}
        summaries={name:browser_summary(report) for name,report in browser_reports.items()}
        # Require measured overlap, do not label an already-finished task as background load.
        intervals=db(runtime,"SELECT started_at,released_at FROM router_attempts WHERE json_extract(envelope_json,'$.stage')='retime_render'")
        busy_rows=browser_reports['isolated-busy']['rows']
        overlap=sum(any(a is not None and b is not None and a<=row['epoch_start_ms']/1000<=b for a,b in intervals) for row in busy_rows)
        assert overlap>=200,'insufficient actual video-processing overlap'
        for summary in summaries.values():assert summary['warm']['count']>=200 and summary['warm']['errors']==0
        pipeline=verify_pipeline(batch,runtime)
        projection=capacity_projection(sum(row['bytes'] for row in upload_rows),directory_usage(runtime/'objects')['logical_bytes'],directory_usage(runtime/'workspace')['logical_bytes'])
        result.update(state='passed',python=sys.executable,production_generation_unchanged=True,production_assets_and_config_unchanged=True,
            elapsed_seconds=time.monotonic()-start,requested_seconds=args.seconds,resource_samples=len(samples),production_probes=request_summary(probe_rows),
            browser=summaries,busy_queries_with_actual_retime_overlap=overlap,uploads=request_summary(upload_rows),uploaded_sources=len(inputs),retime_outputs=len(outputs),
            isolated_runtime=str(runtime),models_enabled=True,formal_terminology_releases=0,expected_clip_state='needs_attention/unpublished_vocabulary',
            reused_ingest_batch=str(prior) if prior else None,current_ingest_requests=0 if prior else 30,
            capacity_projection=projection,peak_service_rss_sum=max(s.get('service_rss_sum',0) for s in samples),
            min_available_ram=min(s['ram_available'] for s in samples),min_disk_free=min(s['disk_free'] for s in samples),
            pipeline_postchecks=pipeline,
            formal_scale_acceptance=False,business_acceptance=False,logout_or_reboot=False,adjacent_project_modified=False)
    except Exception as exc:
        result.update(error_type=type(exc).__name__,error=str(exc)[:200]);raise
    finally:
        shutdown.set();thread.join(20);save(batch/'browser-stop.json',{'stop':True})
        for _,proc,path in browser_jobs:
            try:proc.wait(timeout=20)
            except subprocess.TimeoutExpired:proc.terminate();proc.wait(timeout=10)
            path.unlink(missing_ok=True)
        if private is not None:
            try:stop(runtime,timeout=45);private.wait(timeout=10)
            except Exception:result.update(state='failed',isolated_stop_failed=True)
        save(batch/'resource-samples.json',samples);save(batch/'production-probes.json',probe_rows);save(batch/'activity.json',activity)
        save(batch/'validation.json',result);save(batch/'state.json',{'state':result['state'],'report':str(batch/'validation.json')})
        print(encode({'state':result['state'],'report':str(batch/'validation.json')}),flush=True)
        if result.get('isolated_stop_failed'):raise RuntimeError('isolated_stop_failed; inspect validation.json')


if __name__=='__main__':main()
