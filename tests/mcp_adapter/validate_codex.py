"""Actual installed Codex CLI host + MCP + attached verified image. Synthetic, no editing adoption."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from search.validate_public import main as execute
from media_tag.catalog.service import encode
from media_tag.client.http import LibraryClient,ClientSettings


def extension(env):
    codex=shutil.which('codex')
    assert codex,'installed Codex CLI required'
    folder=env.folder/'codex';folder.mkdir();(folder/'README.md').write_text('Actual installed Codex CLI MCP/image validation. Synthetic geometry only; no medical or editing-use acceptance.\n')
    token=folder/'reader-token.local.txt';token.write_text(env.tokens['reader'])
    config=folder/'reader-client.local.json';config.write_text(encode({'base_url':env.url,'token_file':str(token)}),encoding='utf8')
    facade=LibraryClient(ClientSettings(base_url=env.url,token_file=str(token)))
    manifest=facade.manifest(env.picture_asset.asset_id,env.picture_asset.sha256)
    data=httpx.get(env.url+manifest['content'],headers={'Authorization':'Bearer '+env.tokens['reader']},timeout=15).content
    assert sha256(data).hexdigest()==env.picture_asset.sha256
    picture=folder/'verified-download.png';picture.write_bytes(data)
    schema={'type':'object','additionalProperties':False,'properties':{
        'tools_called':{'type':'array','items':{'type':'string'}},'foreground_shape':{'type':'string','enum':['circle','square','unknown']},
        'foreground_color':{'type':'string','enum':['red','blue','unknown']},'image_description':{'type':'string'},
        'unauthorized_source_rejected':{'type':'boolean'},'medical_use_approved':{'type':'boolean'}},
        'required':['tools_called','foreground_shape','foreground_color','image_description','unauthorized_source_rejected','medical_use_approved']}
    schema_file=folder/'answer-schema.json';schema_file.write_text(encode(schema),encoding='utf8')
    prompt=f'''This is a read-only integration check of the installed media_tag MCP server, not a coding or editing task.
Do not modify files, execute shell commands, create agents, or change any approval or project policy.
Use the actual media_tag MCP tools to perform every step:
1. search_assets query "{env.picture_asset.asset_id}", literal_only=true, use_model=false.
2. get_asset for the returned asset_id.
3. get_asset_preview for its returned asset_id and sha256.
4. get_asset_download for that same fixed version.
5. get_asset for "{env.video_asset.asset_id}". Its access has been revoked; report the real tool error, with no fallback.
The attached image is the actual verified download of step 4, SHA256 {env.picture_asset.sha256}. Describe its visible foreground shape/color.
The preview manifest alone does not prove viewing. Make no video-viewing or medical approval claim.
Return the specified JSON with actual tool names used, image observations and unauthorized_source_rejected based on the tool response. medical_use_approved must remain false.'''
    final=folder/'answer.json';events=folder/'events.jsonl';errors=folder/'stderr.log'
    args=[codex,'exec','--ephemeral','--json','--color','never','--sandbox','read-only','--cd',str(env.root),
        '-c','notify=[]','-c','mcp_servers.node_repl.enabled=false','-c','mcp_servers.media_tag.required=true',
        '-c','mcp_servers.media_tag.args='+json.dumps(['-m','media_tag','mcp-stdio','--config',str(config)]),
        '--output-schema',str(schema_file),'--output-last-message',str(final),'--image',str(picture),'-']
    started=time.monotonic()
    with events.open('w',encoding='utf8') as out,errors.open('w',encoding='utf8') as err:
        result=subprocess.run(args,input=prompt,text=True,encoding='utf8',stdout=out,stderr=err,timeout=240,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==0,'actual Codex host failed; inspect private stderr/events'
    answer=json.loads(final.read_text(encoding='utf8'));assert answer['foreground_shape']=='circle' and answer['foreground_color']=='red'
    assert answer['unauthorized_source_rejected'] and not answer['medical_use_approved']
    stream=[json.loads(line) for line in events.read_text(encoding='utf8').splitlines() if line.strip().startswith('{')]
    calls=[v['item'] for v in stream if v.get('type')=='item.completed' and v.get('item',{}).get('type')=='mcp_tool_call' and v['item'].get('server')=='media_tag']
    names=[c['tool'] for c in calls]
    assert set(names)=={'search_assets','get_asset','get_asset_preview','get_asset_download'} and len(calls)>=5,(names,len(calls))
    assert any(c['tool']=='get_asset' and (c.get('result') or {}).get('structured_content',{}).get('error',{}).get('status')==403 for c in calls)
    text=events.read_text(encoding='utf8')+errors.read_text(encoding='utf8')+final.read_text(encoding='utf8')
    assert all(value not in text for value in env.tokens.values())
    version=subprocess.check_output([codex,'--version'],text=True).strip()
    return {'actual_host':'Codex CLI','codex_version':version,'actual_host_validated':True,'actual_mcp_calls':len(calls),'actual_tool_names':names,
        'verified_input_image_sha256':env.picture_asset.sha256,'image_attachment_sent':True,'observed_shape':answer['foreground_shape'],'observed_color':answer['foreground_color'],
        'unauthorized_asset_denied':True,'no_tokens_in_host_logs':True,'global_config_not_overridden_permanently':True,'elapsed_seconds':time.monotonic()-started,
        'actual_video_viewed':False,'editing_adoption_performed':False,'business_acceptance':False,'medical_use_approved':False}


if __name__=='__main__':raise SystemExit(execute(extension=extension,prefix='d18-codex-host'))
