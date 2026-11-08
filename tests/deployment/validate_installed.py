"""Actual EDY scheduled instance stop/backup/restore/restart and globally configured Codex MCP."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from uuid import uuid4
from media_tag.catalog.service import encode
from media_tag.client.http import LibraryClient,load_settings
from media_tag.deployment.local import load,status
from media_tag.maintenance.backup import snapshot,restore


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,required=True);args=parser.parse_args()
    root,instance=load(args.root);project=Path(instance.project_root)
    folder=project/'jobs'/('d07-installed-'+uuid4().hex[:8]);folder.mkdir()
    (folder/'README.md').write_text('Actual installed current-user Windows scheduled deployment and Codex global MCP offline/restart diagnostics. No production data import or business acceptance.\n')
    codex=shutil.which('codex');pwsh=shutil.which('pwsh') or str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
    assert codex
    secrets=[(root/(name+'-token.local.txt')).read_text() for name in ('owner','worker','reader')]
    original_config_hash=sha256((Path.home()/'.codex/config.toml').read_bytes()).hexdigest()
    def task(action):
        run=subprocess.run([pwsh,'-NoProfile','-NonInteractive','-File',str(project/'scripts/windows/local-service.ps1'),'-Action',action,'-Root',str(root)],capture_output=True,text=True,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW)
        assert run.returncode==0,'installed scheduled operation failed'
        values=[json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
        return values[-1]
    def ready():
        deadline=time.monotonic()+45
        while time.monotonic()<deadline:
            value=status(root)
            if value['state']=='running':
                LibraryClient(load_settings(root/'reader-client.local.json')).require_reader(search=True);return value
            time.sleep(.2)
        raise AssertionError('installed service startup timeout')
    def host(name,expect_error):
        out=folder/(name+'.events.jsonl');err=folder/(name+'.stderr.log');final=folder/(name+'.answer.txt')
        prompt='Read-only media_tag MCP connectivity check. Do not edit files, run shell commands or delegate. Call media_tag search_assets exactly once with query "deployment-global-no-material-6cd832ea", literal_only=true, use_model=false. Report the actual structured result or exact tool error. An unavailable service is not no_match. Do not use other servers or approve medical use.'
        with out.open('w',encoding='utf8') as stdout,err.open('w',encoding='utf8') as stderr:
            run=subprocess.run([codex,'exec','--ephemeral','--json','--color','never','--sandbox','read-only','--cd',str(project),
                '-c','notify=[]','-c','mcp_servers.node_repl.enabled=false','-c','mcp_servers.media_tag.required=true',
                '--output-last-message',str(final),'-'],input=prompt,text=True,encoding='utf8',stdout=stdout,stderr=stderr,timeout=180,creationflags=subprocess.CREATE_NO_WINDOW)
        assert run.returncode==0,'global Codex host check failed'
        events=[json.loads(line) for line in out.read_text(encoding='utf8').splitlines() if line.startswith('{')]
        calls=[v['item'] for v in events if v.get('type')=='item.completed' and v.get('item',{}).get('type')=='mcp_tool_call']
        assert len(calls)==1 and calls[0]['server']=='media_tag' and calls[0]['tool']=='search_assets'
        value=calls[0]['result']['structured_content']
        if expect_error:assert value['error']['code']=='upstream_unavailable' and value['error']['retryable']
        else:assert value['items']==[] and value['schema_version']=='d17-search-v1'
        content=out.read_text(encoding='utf8')+err.read_text(encoding='utf8')+final.read_text(encoding='utf8')
        assert all(token not in content for token in secrets)
        return {'tool':calls[0]['tool'],'status':calls[0]['status'],'error':value.get('error'),'empty_matches':value.get('items')==[]}
    first=ready();task_before=task('Status')
    try:
        stopped=task('Stop');assert not status(root)['running']
        backup=snapshot(root/'catalog.sqlite3',root/'workspace',root/'objects',folder/'backup',offline_confirmed=True)
        restored=restore(folder/'backup',folder/'restored',backup['manifest_sha256'])
        offline=host('offline',True)
        task('Start');second=ready();online=host('restarted',False)
        assert first['generation']!=second['generation']
        assert sha256((Path.home()/'.codex/config.toml').read_bytes()).hexdigest()==original_config_hash
        result={'state':'passed','instance_id':instance.instance_id,'task':task_before,'stop_task':stopped,'url':f'http://127.0.0.1:{instance.port}/ui/',
            'generations':[first['generation'],second['generation']],'backup':backup,'restore':restored,'actual_codex_global_mcp_calls':[offline,online],
            'global_codex_config_preserved':True,'private_tokens_absent_from_host_logs':True,'left_running':status(root)['running'],
            'formal_medical_roles_assigned':False,'production_catalogue_seeded':False,'business_acceptance':False,'actual_windows_logoff_logon_tested':False}
        (folder/'validation.json').write_text(encode(result),encoding='utf8');print(json.dumps({'state':'passed','report':str(folder/'validation.json')}));return 0
    finally:
        if not status(root)['running']:task('Start');ready()


if __name__=='__main__':raise SystemExit(main())
