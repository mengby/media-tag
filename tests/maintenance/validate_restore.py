"""Read an already stopped synthetic source; real CLI backup/verify/restore + restored HTTP."""
from contextlib import closing
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import platform
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit
from uuid import uuid4
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from terminology.lexicon.validate_http import server,FINGERPRINT
from media_tag.api.config import LocalConfig,load_application
from media_tag.catalog.service import encode
from media_tag.contracts.templates import load_template_bundle


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source-job',type=Path);args=parser.parse_args()
    root=Path.cwd()
    candidates=sorted((root/'jobs').glob('d20-rehearsal-*/validation.json'),key=lambda p:p.stat().st_mtime)
    if not args.source_job and not candidates:raise ValueError('run tests/integration/validate_preview.py first')
    source=args.source_job.resolve() if args.source_job else candidates[-1].parent
    assert source.is_relative_to(root/'jobs'),'synthetic diagnostic sources must remain inside this repository jobs'
    report_source=json.loads((source/'validation.json').read_text(encoding='utf8'))
    assert report_source['state']=='passed' and report_source['synthetic_engineering_only']
    old_origin=json.loads((source/'rehearsal/reader.local.json').read_text(encoding='utf8'))['base_url'];parsed=urlsplit(old_origin)
    with closing(socket.socket()) as probe:assert probe.connect_ex((parsed.hostname,parsed.port))!=0,'source diagnostic server must already be stopped'
    folder=root/'jobs'/('d07-recovery-'+uuid4().hex[:8]);folder.mkdir();(folder/'README.md').write_text('D07 isolated offline restore diagnostic from a stopped synthetic D20 job. No production service deployment or business acceptance.\n')
    config=LocalConfig.model_validate_json((source/'validation.local.json').read_bytes())
    with closing(sqlite3.connect(config.database)) as connection:
        before={'assets':connection.execute('SELECT asset_id,blob_sha256,revision FROM assets ORDER BY asset_id').fetchall(),
            'fields':connection.execute('SELECT asset_id,field,payload FROM effective_values ORDER BY asset_id,field').fetchall(),
            'permissions':connection.execute('SELECT * FROM asset_permissions ORDER BY asset_id,subject_id').fetchall(),
            'variants':connection.execute('SELECT asset_id,source_sha256,output_sha256,time_map_json FROM variants ORDER BY asset_id').fetchall(),
            'serving':connection.execute('SELECT * FROM terminology_serving_head').fetchall()}
    def command(*arguments):
        run=subprocess.run([sys.executable,'-m','media_tag',*arguments],capture_output=True,timeout=90,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        assert run.returncode==0,(arguments[0],run.stderr.decode()[:300]);return json.loads(run.stdout)
    backup=command('backup-offline','--config',str(source/'validation.local.json'),'--destination',str(folder/'backup'),'--offline-confirmed')
    verified=command('verify-backup','--source',str(folder/'backup'),'--manifest-sha256',backup['manifest_sha256'])
    restored=command('restore-offline','--source',str(folder/'backup'),'--destination',str(folder/'restored'),'--manifest-sha256',backup['manifest_sha256'])
    with closing(sqlite3.connect(folder/'restored/database.sqlite3')) as connection:
        after={'assets':connection.execute('SELECT asset_id,blob_sha256,revision FROM assets ORDER BY asset_id').fetchall(),
            'fields':connection.execute('SELECT asset_id,field,payload FROM effective_values ORDER BY asset_id,field').fetchall(),
            'permissions':connection.execute('SELECT * FROM asset_permissions ORDER BY asset_id,subject_id').fetchall(),
            'variants':connection.execute('SELECT asset_id,source_sha256,output_sha256,time_map_json FROM variants ORDER BY asset_id').fetchall(),
            'serving':connection.execute('SELECT * FROM terminology_serving_head').fetchall()}
        assert before==after
    private=folder/'restored-runtime.local.json'
    payload=config.model_dump(mode='json')|{'database':str(folder/'restored/database.sqlite3'),'workspace':str(folder/'restored/workspace'),'object_store':str(folder/'restored/objects')}
    private.write_text(encode(payload),encoding='utf8')
    app,router,tools=load_application(private)
    token={actor:(source/'rehearsal'/(actor+'-token.local.txt')).read_text() for actor in ('reader','derived')}
    picture=next(a for a in before['assets'] if a[1] in report_source['extension']['task_local_download_hashes'] and a[1]!=report_source['derived_sha256'])
    derived=next(a for a in before['assets'] if a[1]==report_source['derived_sha256'])
    source_video=next(a for a in before['assets'] if a[1]==report_source['download_sha256'])
    count=0
    with server(app) as origin,httpx.Client(base_url=origin,timeout=30) as client:
        def call(path,*,actor='reader',status=200,method='GET',body=None):
            nonlocal count
            count+=1;response=client.request(method,path,headers={'Authorization':'Bearer '+token[actor]},json=body)
            assert response.status_code==status,(path,response.status_code)
            return response
        assert call('/v1/assets/search',method='POST',body={'query':'合成圆圈'}).json()['items']
        model=call('/v1/assets/search',method='POST',body={'query':'红色的合成圆盘图形'}).json();assert model['resolution']['model_state']=='used'
        manifest=call(f'/v1/assets/{picture[0]}/download-manifest?sha256={picture[1]}').json()
        data=call(manifest['content']).content;assert sha256(data).hexdigest()==picture[1]
        manifest=call(f'/v1/assets/{derived[0]}/download-manifest?sha256={derived[1]}',actor='derived').json()
        assert manifest['transform']['cumulative_speed']==2 and manifest['technical']['duration_seconds']==.5
        assert sha256(call(manifest['content'],actor='derived').content).hexdigest()==derived[1]
        call(f'/v1/assets/{source_video[0]}/download-manifest?sha256={source_video[1]}',actor='derived',status=403)
        call(f'/v1/assets/{source_video[0]}/download-manifest?sha256={source_video[1]}',status=403)
        call(f'/v1/assets/{picture[0]}/download-manifest?sha256={"f"*64}',status=412)
    assert load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT).fingerprint==FINGERPRINT
    assert sha256((source/'originals/circle.png').read_bytes()).hexdigest()==picture[1]
    assert sha256((source/'originals/growth.mp4').read_bytes()).hexdigest()==source_video[1]
    # Original source files and its logical state remain unchanged by the read-only backup.
    with closing(sqlite3.connect(config.database)) as connection:
        assert connection.execute('SELECT asset_id,blob_sha256,revision FROM assets ORDER BY asset_id').fetchall()==before['assets']
        assert connection.execute('SELECT asset_id,field,payload FROM effective_values ORDER BY asset_id,field').fetchall()==before['fields']
    result={'state':'passed','synthetic_engineering_only':True,'interpreter':sys.executable,'source_job':source.name,'source_server_stopped':True,
        'local_environment':{'platform':platform.platform(),'python':platform.python_version(),'base_executable':getattr(sys,'_base_executable',None),
            'project_venv':sys.prefix!=sys.base_prefix,'disk_free_bytes_after_restore':shutil.disk_usage(root).free,'final_target_machine_confirmed':False},
        'backup':backup,'verify':verified,'restore':restored,'restored_http_requests':count,'real_bge_cpu_after_restore':True,'assets_fields_permissions_variants_serving_equal':True,
        'original_assets_and_values_unchanged':True,'template_fingerprint':FINGERPRINT,'derived_speed_preserved':2.0,'denied_source_still_denied':True,
        'final_deployment_accepted':False,'cross_machine_validated':False,'business_acceptance':False}
    (folder/'validation.json').write_text(encode(result),encoding='utf8');print(json.dumps({'state':'passed','report':str(folder/'validation.json')}));return 0


if __name__=='__main__':raise SystemExit(main())
