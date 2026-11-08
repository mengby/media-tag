"""Real official SDK subprocesses + authenticated API + CPU BGE, synthetic engineering only."""
import asyncio
import json
from pathlib import Path
import socket
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mcp import Client,StdioServerParameters,stdio_client
from mcp_adapter.test_stdio import alive
from search.validate_public import main as execute
from media_tag.catalog.service import encode


def extension(env):
    def business_snapshot():
        with env.router.database.transaction() as c:
            return {'assets':[tuple(r) for r in c.execute('SELECT asset_id,revision,blob_sha256 FROM assets ORDER BY asset_id')],
                'fields':[tuple(r) for r in c.execute('SELECT asset_id,field,payload FROM effective_values ORDER BY asset_id,field')],
                'jobs':[tuple(r) for r in c.execute('SELECT job_id,cancelled FROM router_jobs ORDER BY job_id')],
                'publication':dict(c.execute('SELECT * FROM terminology_head').fetchone())}
    before=business_snapshot()
    folder=env.folder/'mcp';folder.mkdir();(folder/'README.md').write_text('Real D18 MCP stdio protocol diagnostics. Synthetic geometry, no AI host or medical acceptance.\n')
    configurations={}
    for actor in ('reader','derived','owner'):
        token=folder/(actor+'-token.local.txt');token.write_text(env.tokens[actor],encoding='utf8')
        config=folder/(actor+'.local.json');config.write_text(encode({'base_url':env.url,'token_file':str(token)}),encoding='utf8');configurations[actor]=config
    calls=0;protocols=[];child_pids=[];observations=[]
    async def connect(actor,mode,body,config=None):
        nonlocal calls
        suffix=actor+'-'+mode+'-'+str(len(child_pids));marker=folder/(suffix+'.pid');log=folder/(suffix+'.stderr.log')
        parameters=StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('stdio_entry.py')),str(config or configurations[actor]),str(marker)],
            cwd=str(env.root),env={'PYTHONUTF8':'1'})
        with log.open('w',encoding='utf8') as errors:
            async with Client(stdio_client(parameters,errlog=errors),mode=mode,read_timeout_seconds=30) as client:
                listing=await client.list_tools();assert {t.name for t in listing.tools}=={'search_assets','get_asset','get_asset_preview','get_asset_download'}
                assert all(t.annotations.read_only_hint and t.annotations.open_world_hint is False for t in listing.tools)
                protocols.append(client.protocol_version)
                async def call(name,args,*,error=None):
                    nonlocal calls
                    calls+=1;response=await client.call_tool(name,args)
                    if error:
                        assert response.is_error and response.structured_content['error']['code']==error
                    else:assert not response.is_error
                    return response.structured_content
                await body(call)
        pid=int(marker.read_text());child_pids.append(pid);assert not alive(pid)
        text=log.read_text(encoding='utf8');assert all(token not in text for token in env.tokens.values())
    async def run():
        for mode in ('2026-07-28','legacy'):
            async def reader(call):
                searched=await call('search_assets',{'query':'合成圆圈','limit':10});assert len(searched['items'])==1
                item=searched['items'][0];assert item['asset_id']==env.picture_asset.asset_id and item['sha256']==env.picture_asset.sha256
                details=await call('get_asset',{'asset_id':item['asset_id']});assert details['current_use_review']=='not_checked'
                preview=await call('get_asset_preview',{'asset_id':item['asset_id'],'sha256':item['sha256']});assert not preview['full_video_observed']
                manifest=await call('get_asset_download',{'asset_id':item['asset_id'],'sha256':item['sha256']});assert manifest['whole_file'] and manifest['size_bytes']==env.picture.stat().st_size
                await call('get_asset',{'asset_id':env.video_asset.asset_id},error='file_access_denied')
                await call('search_assets',{'query':'x','user_id':'admin'},error='invalid_tool_input')
                await call('publish_terms',{},error='unknown_read_only_tool')
                missing=await call('search_assets',{'query':'工程库未收录的航班登机牌','use_model':False});assert not missing['items'] and missing['resolution']['state']=='no_match'
                model=await call('search_assets',{'query':'红色的合成圆盘图形'});assert model['resolution']['model_state']=='used'
                observations.append({'protocol':mode,'model_state':model['resolution']['model_state'],'cache_hit':model['resolution']['cache_hit'],
                    'resource_usage':model['resolution'].get('resource_usage'),'model_candidates_not_auto_selected':not model['selected_concept_ids']})
            await connect('reader',mode,reader)
        async def derived(call):
            record=env.derived_asset
            manifest=await call('get_asset_download',{'asset_id':record.asset_id,'sha256':record.sha256})
            assert manifest['transform']['cumulative_speed']==2 and not manifest['transform']['apply_transform_again']
            preview=await call('get_asset_preview',{'asset_id':record.asset_id,'sha256':record.sha256,'start_seconds':.0625,'end_seconds':.25})
            assert preview['requested_playback']['sha256']==record.sha256 and not preview['requested_playback']['server_clip_generated']
            await call('get_asset_download',{'asset_id':env.video_asset.asset_id,'sha256':env.video_asset.sha256},error='file_access_denied')
        await connect('derived','2026-07-28',derived)
        async def owner(call):await call('search_assets',{'query':'合成圆圈'},error='reader_credential_required')
        await connect('owner','2026-07-28',owner)
        sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
        offline=folder/'offline.local.json';offline.write_text(encode({'base_url':f'http://127.0.0.1:{port}','token_file':str(folder/'reader-token.local.txt')}),encoding='utf8')
        async def unavailable(call):await call('search_assets',{'query':'合成圆圈'},error='upstream_unavailable')
        await connect('reader','2026-07-28',unavailable,config=offline)
    asyncio.run(run())
    assert business_snapshot()==before
    report={'mcp_tool_calls':calls,'protocols':protocols,'child_pids_exited':child_pids,'model_observations':observations,
        'real_stdio_subprocesses':len(child_pids),'real_cpu_bge_call':True,'write_tools_exposed':False,'credential_echo_found':False,
        'derived_only_scope_preserved':True,'upstream_unavailable_not_no_match':True,'ai_host_validated':False,'actual_media_viewed_by_ai':False,
        'business_values_revisions_jobs_and_publication_unchanged':True,'business_acceptance':False}
    (folder/'validation.json').write_text(encode(report),encoding='utf8')
    return report


if __name__=='__main__':raise SystemExit(execute(extension=extension,prefix='d18-mcp'))
