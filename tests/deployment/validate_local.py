"""Actual Windows supervised API/Worker upload, in-flight GPU crash/recovery and offline restore."""
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4
from PIL import Image,ImageDraw
import httpx
import psutil
from media_tag.catalog.service import encode
from media_tag.deployment.local import initialize,status,stop
from media_tag.maintenance.backup import snapshot,restore


def main():
    project=Path.cwd();identity=uuid4().hex[:8];folder=project/'jobs'/('d07-local-'+identity);folder.mkdir()
    (folder/'README.md').write_text('Private D07 real local supervised Windows/HTTP/model/crash/restore validation. Synthetic engineering, no formal vocabulary or medical/privacy approval.\n')
    runtime=project/'data'/('local-validation-'+identity)
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    initialized=initialize(runtime,project,port=port)
    processes=[];http_count=0
    def launch():
        log=(folder/('supervisor-'+str(len(processes))+'.stderr.log')).open('w',encoding='utf8')
        p=subprocess.Popen([sys.executable,'-m','media_tag','local-run','--root',str(runtime)],cwd=project,stdout=subprocess.DEVNULL,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        log.close();processes.append(p);return p
    def wait_for(predicate,seconds=45):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            value=predicate()
            if value:return value
            time.sleep(.025)
        raise AssertionError('local validation condition timeout')
    def ready():
        value=status(runtime)
        if value['state']=='failed':raise AssertionError(value)
        return value if value['running'] and value['state']=='running' else None
    def rows(sql,parameters=()):
        with closing(sqlite3.connect(runtime/'catalog.sqlite3')) as c:return c.execute(sql,parameters).fetchall()
    tokens={kind:(runtime/(kind+'-token.local.txt')).read_text() for kind in ('owner','reader','worker')}
    with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=20) as client:
        def call(method,path,payload=None,*,actor='owner',expected=200,content=None,headers=None):
            nonlocal http_count
            http_count+=1;response=client.request(method,path,json=payload,content=content,headers={'Authorization':'Bearer '+tokens[actor],**(headers or {})})
            assert response.status_code==expected,(method,path,response.status_code,response.text[:200]);return response
        try:
            supervisor=launch();first=wait_for(ready)
            duplicate=subprocess.run([sys.executable,'-m','media_tag','local-run','--root',str(runtime)],capture_output=True,timeout=10)
            assert duplicate.returncode==2 and json.loads(duplicate.stderr)['error']=='deployment_already_running'
            assert ready()['generation']==first['generation']
            original=folder/'engineering.png';image=Image.new('RGB',(256,128),'white');draw=ImageDraw.Draw(image);draw.ellipse((12,24,92,104),fill='red');draw.text((108,50),'LOCAL CHECK',fill='black');image.save(original)
            data=original.read_bytes();digest=sha256(data).hexdigest()
            upload=call('POST','/uploads',{'request_id':'local-real','filename':'engineering.png','size_bytes':len(data),'sha256':digest}).json()
            call('PUT',f"/uploads/{upload['upload_id']}/chunks?offset=0",content=data,headers={'X-Chunk-SHA256':digest})
            asset=call('POST',f"/uploads/{upload['upload_id']}/complete").json()['asset_id']
            def clip_running():return rows("SELECT t.task_id,t.lease_epoch FROM router_tasks t JOIN router_attempts a USING(task_id,lease_epoch) WHERE json_extract(t.spec_json,'$.stage')='clip' AND t.state IN ('leased','running') AND a.released_at IS NULL")
            active=wait_for(clip_running,60)[0]
            assert rows("SELECT count(*) FROM router_holds WHERE resource='gpu:0'")[0][0]==1
            descendants=psutil.Process(supervisor.pid).children(recursive=True)
            supervisor.kill();supervisor.wait(10)
            gone,remaining=psutil.wait_procs(descendants,timeout=10)
            assert not remaining,[(p.pid,p.name()) for p in remaining]
            # Reopening the same named job on restart fences descendants before confirm_stopped.
            supervisor=launch();second=wait_for(ready)
            assert second['generation']!=first['generation']
            assert rows('SELECT released_at FROM router_attempts WHERE task_id=? AND lease_epoch=?',active)[0][0] is not None
            def done():
                values=rows("SELECT json_extract(spec_json,'$.stage'),state,error_code FROM router_tasks")
                assert all(v[1] not in ('failed','needs_attention') for v in values if v[0]!='clip'),values
                if any(v[0]=='clip' and v[1]=='needs_attention' for v in values):
                    payload=json.loads(rows('SELECT result_json FROM router_attempts WHERE task_id=? AND lease_epoch=2',(active[0],))[0][0])
                    assert payload['status']=='partial' and {e['code'] for e in payload['errors']}=={'unpublished_vocabulary'}
                return values if values and all(v[1]=='succeeded' or v[0]=='clip' and v[1]=='needs_attention' for v in values) else None
            wait_for(done,120)
            assert rows('SELECT count(*) FROM router_attempts WHERE task_id=?',(active[0],))[0][0]==2
            assert all(attempt==1 for stage,attempt in rows("SELECT json_extract(spec_json,'$.stage'),attempt FROM router_tasks") if stage!='clip')
            assert rows('SELECT count(*) FROM router_holds')[0][0]==0
            details=call('GET','/v1/assets/'+asset).json()
            call('POST','/ui/library/'+asset,{'request_id':'share-synthetic','expected_revision':details['revision'],'output_sha256':digest,'active':True,'recipients':['local-codex-reader']})
            manifest=call('GET',f'/v1/assets/{asset}/download-manifest?sha256={digest}',actor='reader').json()
            assert sha256(call('GET',manifest['content'],actor='reader').content).hexdigest()==digest
            call('POST','/uploads',{'request_id':'reader-denied','filename':'engineering.png','size_bytes':len(data),'sha256':digest},actor='reader',expected=403)
            assert call('GET','/v1/capabilities',actor='reader').json()['read_only_identity']
            stop(runtime);supervisor.wait(10)
            backup=snapshot(runtime/'catalog.sqlite3',runtime/'workspace',runtime/'objects',folder/'backup',offline_confirmed=True)
            restored=restore(folder/'backup',folder/'restored',backup['manifest_sha256'])
            assert sha256(original.read_bytes()).hexdigest()==digest
            with closing(sqlite3.connect(runtime/'catalog.sqlite3')) as c:
                assert c.execute('SELECT count(*) FROM index_confirmations').fetchone()[0]==0
                assert c.execute('SELECT count(*) FROM privacy_reviews').fetchone()[0]==0
                assert c.execute('SELECT count(*) FROM terminology_releases').fetchone()[0]==0
            result={'state':'passed','synthetic_engineering_only':True,'instance_id':initialized['instance_id'],'runtime_root':str(runtime),
                'http_requests':http_count,'source_sha256':digest,'asset_id':asset,'supervisor_generations':[first['generation'],second['generation']],
                'inflight_gpu_attempt_killed':list(active),'old_attempt_released_after_os_tree_fence':True,'only_interrupted_stage_retried':True,
                'fixed_download_verified':True,'reader_write_denied':True,'duplicate_supervisor_refused':True,'backup':backup,'restore':restored,
                'formal_terminology_releases':0,'medical_approvals':0,'privacy_approvals':0,'business_acceptance':False}
            result['clip_state']='partial_unpublished_vocabulary_expected'
            result['all_old_descendants_exited_before_restart']=[p.pid for p in gone]
            (folder/'validation.json').write_text(encode(result),encoding='utf8');print(json.dumps({'state':'passed','report':str(folder/'validation.json')}));return 0
        finally:
            try:stop(runtime,timeout=40)
            except Exception:pass
            for process in processes:
                if process.poll() is None:process.kill()
                process.wait(timeout=10)


if __name__=='__main__':raise SystemExit(main())
