"""D20 local rehearsal: actual stdio/API/bytes/render, explicitly not target-host acceptance."""
import asyncio
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from mcp import Client,StdioServerParameters
from search.validate_public import main as execute
from media_tag.catalog.service import encode
from media_tag.client.http import ClientSettings,LibraryClient
from media_tag.client.models import Acquisition,CurrentUseCheck
from media_tag.client.provider import TaskProvider
from media_tag.search.public import AssetSearch


def extension(env):
    folder=env.folder/'rehearsal';folder.mkdir();(folder/'README.md').write_text('D20 local synthetic engineering only: actual stdio/HTTP/download/render, no target host or medical/current-use acceptance.\n')
    jobs=folder/'jobs';jobs.mkdir();(jobs/'README.md').write_text('New isolated synthetic preview jobs, never adjacent projects.\n')
    clients={};configs={}
    for actor in ('reader','derived'):
        token=folder/(actor+'-token.local.txt');token.write_text(env.tokens[actor])
        settings=ClientSettings(base_url=env.url,token_file=str(token));clients[actor]=LibraryClient(settings)
        configs[actor]=folder/(actor+'.local.json');configs[actor].write_text(encode(settings.model_dump(mode='json')),encoding='utf8')
    mcp_calls=0;tool_results={}
    async def tools():
        nonlocal mcp_calls
        for actor,asset in (('reader',env.picture_asset),('derived',env.derived_asset)):
            parameters=StdioServerParameters(command=sys.executable,args=['-m','media_tag','mcp-stdio','--config',str(configs[actor])],cwd=str(env.root))
            async with Client(parameters,mode='2026-07-28',read_timeout_seconds=30) as sdk:
                spec=AssetSearch(query=asset.asset_id,literal_only=True)
                direct=clients[actor].search(spec)
                response=await sdk.call_tool('search_assets',spec.model_dump(mode='json',exclude_defaults=True));mcp_calls+=1
                assert not response.is_error
                bridged=response.structured_content
                (folder/(actor+'-api.json')).write_text(encode(direct),encoding='utf8')
                (folder/(actor+'-mcp.json')).write_text(encode(bridged),encoding='utf8')
                def stable(value):return value|{'resolution':{k:v for k,v in value['resolution'].items() if k not in ('cache_hit','elapsed_seconds')}}
                assert stable(bridged)==stable(direct)
                item=direct['items'][0];assert item['sha256']==asset.sha256
                values={}
                for name in ('get_asset_preview','get_asset_download'):
                    result=await sdk.call_tool(name,{'asset_id':asset.asset_id,'sha256':asset.sha256});mcp_calls+=1
                    assert not result.is_error;values[name]=result.structured_content
                assert not values['get_asset_preview']['full_video_observed'] and values['get_asset_download']['whole_file']
                tool_results[actor]={'search':direct,**values}
    asyncio.run(tools())
    providers={actor:TaskProvider(clients[actor],jobs,'preview-task',env.tools,license_validation=False) for actor in clients}
    task=providers['reader'].store.task
    transcript=b'{"synthetic_geometry_script":"show a growing circle, then a still circle"}'
    candidate=b'{"synthetic_candidate":"derived 2x video plus still image, no further speed change"}'
    (task/'edit/cut.transcript.json').write_bytes(transcript);(task/'edit/candidate.json').write_bytes(candidate)
    results=[];handoffs=[]
    for actor,asset,window in (('derived',env.derived_asset,(0.0,.5)),('reader',env.picture_asset,(.5,1.5))):
        provider=providers[actor]
        spec=Acquisition(asset_id=asset.asset_id,sha256=asset.sha256,search_versions=tool_results[actor]['search']['search_versions'],context={
            'unit_id':actor+'-shape','transcript_sha256':sha256(transcript).hexdigest(),'candidate_sha256':sha256(candidate).hexdigest(),
            'quote':'Synthetic geometry demonstration','purpose':'Show growing circle' if actor=='derived' else 'Show a still circle',
            'timeline':{'start_seconds':window[0],'end_seconds':window[1]},
            'source_interval':{'start_seconds':0.0,'end_seconds':.5} if actor=='derived' else None})
        result=provider.fetch(spec);assert provider.fetch(spec)['receipt_sha256']==result['receipt_sha256'];results.append(result)
        check=CurrentUseCheck.model_validate_json(encode({'receipt_sha256':result['receipt_sha256'],'context':spec.context.model_dump(mode='json'),
            'source_sha256':asset.sha256,'decision':'pass','reviewer_type':'model','reviewed_by':'synthetic-contract-fixture','reviewed_at':datetime.now(timezone.utc).isoformat(),
            'actual_media_observed':True,'evidence':[{'path':result['receipt']['src'],'sha256':asset.sha256}],
            'checks':{k:{'status':'skipped' if k=='license' else 'pass','observation':'Synthetic protocol fixture only; not a real AI host observation or business acceptance.'}
                for k in ('semantic','action','timing','composition','packaging','technical','style','source','license','eye_mosaic')}}))
        handoff=provider.prepare_preview(check)['handoff'];assert handoff['preview_only'] and not handoff['final_export_allowed'];handoffs.append(handoff)
    video,image=(r['local_path'] for r in results);preview=task/'edit/preview.mp4'
    env.tools.ffmpeg_run(['-v','error','-i',video,'-loop','1','-t','1','-i',image,'-filter_complex',
        '[0:v]fps=16,scale=192:128,setsar=1,setpts=PTS-STARTPTS[a];[1:v]fps=16,scale=192:128,setsar=1,setpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=1:a=0[v]',
        '-map','[v]','-c:v','libx264','-pix_fmt','yuv420p',str(preview)],timeout=30)
    env.tools.ffmpeg_run(['-v','error','-xerror','-i',str(preview),'-f','null','-'],timeout=20)
    metadata=env.tools.probe_run(preview,timeout=20,extra=['-show_format','-show_streams','-count_frames'])
    assert float(metadata['format']['duration'])==1.5 and int(metadata['streams'][0]['nb_read_frames'])==24
    assert results[0]['receipt']['transform']['cumulative_speed']==2.0 and not handoffs[0]['apply_transform_again']
    (folder/'rehearsal.json').write_text(encode({'tool_results':tool_results,'receipts':results,'handoffs':handoffs,'preview_metadata':metadata}),encoding='utf8')
    return {'local_engineering_rehearsal':'passed','actual_stdio_children':2,'actual_mcp_calls':mcp_calls,'api_mcp_same_identity_results_equal':True,
        'task_local_download_hashes':[r['receipt']['sha256'] for r in results],'task_local_handoffs':len(handoffs),'current_use_checks':'synthetic_protocol_fixtures_only',
        'full_preview_relative':preview.relative_to(env.root).as_posix(),'full_preview_sha256':sha256(preview.read_bytes()).hexdigest(),'full_preview_seconds':1.5,'decoded_video_frames':24,
        'additional_speed_applied':False,'actual_ai_host_validated':False,'actual_media_observed_by_ai':False,'cross_machine_validated':False,
        'adjacent_project_written':False,'final_export_allowed':False,'medical_status':'pending','business_acceptance':False,
        'formal_I01_I08_acceptance':'pending_target_host_business_data_and_deployment'}


if __name__=='__main__':raise SystemExit(execute(extension=extension,prefix='d20-rehearsal'))
