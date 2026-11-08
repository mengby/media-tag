"""Explicit real-task D20 phases. No manufactured viewing, acceptance or medical approval."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import httpx
from uuid import uuid4
from media_tag.catalog.service import encode
from media_tag.client.editing import EditingSource
from media_tag.contracts.models import validate_relative_path
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import Toolchain


ROOT=Path(__file__).resolve().parents[2]
HOST=Path('D:/ai/auto-video')


def digest(path):
    h=sha256()
    with Path(path).open('rb') as stream:
        while chunk:=stream.read(1024**2):h.update(chunk)
    return h.hexdigest()


def save(path,value):path.write_text(encode(value),encoding='utf8')
def read(path):return json.loads(path.read_text(encoding='utf8'))


def directory(path):
    missing=[];p=path
    while not p.exists():missing.append(p);p=p.parent
    for p in reversed(missing):
        p.mkdir();(p/'README.md').write_text('D20 task-local work or read-only input copies. Prior approvals are not inherited; medicine remains pending.\n',encoding='utf8')


def snapshot(task):
    values={}
    for p in sorted(task.rglob('*')):
        if p.is_symlink():raise ValueError('linked source inputs are not supported')
        if p.is_file() and not p.name.startswith('.env'):
            FilesystemBackend(task)._resolve(p.relative_to(task).as_posix())
            values[p.relative_to(task).as_posix()]=digest(p)
    return values


def command(batch,script,*args,timeout=900):
    log=batch/(script.replace('.py','')+'-'+uuid4().hex[:6]+'.log')
    env=os.environ|{'PYTHONUTF8':'1','EDITOR_PYTHON':sys.executable,
        'PATH':str(ROOT/'cache/media-tools/ffmpeg-9.0.2/bin')+os.pathsep+os.environ.get('PATH',''),
        'BROWSER_EXECUTABLE':'C:/Program Files/Google/Chrome/Application/chrome.exe'}
    with log.open('w',encoding='utf8') as stream:
        result=subprocess.run([sys.executable,str(HOST/'scripts'/script),*map(str,args)],cwd=HOST,env=env,
            stdout=stream,stderr=subprocess.STDOUT,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:raise RuntimeError('host command failed; '+str(log))
    return log


def copy_inputs(src,edit):
    selected=['cut.preview.mp4','cut.preview.report.json','cut.transcript.json','audio.processing.json','style.resolved.json','preparation.json','editorial-execution.json']
    # Follow real evidence/file references, preserving hashes. Never copy approval/acceptance sidecars.
    plan=read(src/'preparation.json')
    queue=[*selected,plan['evidence_checks']];visited=set();copied=[]
    while queue:
        key=queue.pop(0)
        key=key.replace('\\','/')
        if key in visited:continue
        visited.add(key)
        try:validate_relative_path(key)
        except ValueError:continue
        if any(x in key.lower() for x in ('.env','approval','approved-','acceptance','authorizations','av-review')) or key.endswith(('.local.json','.local.txt')):continue
        try:p=FilesystemBackend(src)._resolve(key)
        except Exception:continue
        if len(copied)>=512:raise ValueError('input reference budget exceeded')
        dest=edit/key;directory(dest.parent);shutil.copyfile(p,dest);copied.append(key)
        if p.suffix=='.json' and p.stat().st_size<8*1024**2:
            try:data=read(p)
            except (ValueError,UnicodeError):continue
            def references(value):
                if isinstance(value,dict):
                    for k,v in value.items():
                        if k in ('path','source_file','text_file') and isinstance(v,str):queue.append(v)
                        elif isinstance(v,(dict,list)):references(v)
                elif isinstance(value,list):
                    for v in value:references(v)
            references(data)
    return copied


def prepare(source_id):
    if re.fullmatch('[A-Za-z0-9][A-Za-z0-9_-]*',source_id) is None:raise ValueError('invalid source task')
    original=HOST/'jobs'/source_id;src=original/'edit'
    if not (original/'script.md').is_file() or not (src/'edit.json').is_file():raise ValueError('real script and edit.json required')
    policy=read(HOST/'config/workflow-policy.json')
    if policy.get('preview_only') is not True:raise ValueError('preview-only policy required')
    batch=ROOT/'jobs'/('d20-real-'+uuid4().hex[:8]);batch.mkdir()
    (batch/'README.md').write_text('D20 real selected task integration. Source is read only; medical and full listening review remain pending. No final export.\n',encoding='utf8')
    before=snapshot(original);save(batch/'source-snapshot.json',before)
    bridge=EditingSource(HOST/'config/media-tag.local.json');identity='d20-'+source_id+'-'+uuid4().hex[:8]
    bridge.init_job(identity);task=bridge.task(identity);edit=task/'edit';run=edit/'automation/runs/d20';directory(run)
    for name in ('job.yaml','script.md'):shutil.copyfile(original/name,task/name)
    copied=copy_inputs(src,edit)
    props=read(src/'edit.json');props['media']=[]
    save(run/'candidate.json',props)
    save(edit/'auto.settings.json',read(src/'auto.settings.json'))
    (edit/'input-script.md').write_bytes((original/'script.md').read_bytes())
    # The exact prior evidence/plan is an input, not a new medical or viewing approval.
    save(edit/'d20-input-lineage.json',{'source_task':source_id,'copied_inputs':copied,'source_edit_sha256':before['edit/edit.json'],
        'script_sha256':before['script.md'],'old_approvals_inherited':False,'medical_status':'pending','new_use_checks_required':True})
    policy_paths=['config/workflow-policy.json','config/media-sources.json','scripts/render_job.py','scripts/check_edit.py']
    state={'schema_version':'d20-real-v1','source_task':source_id,'target_job':identity,'batch':str(batch),'task':str(task),
        'policy_hashes':{k:digest(HOST/k) for k in policy_paths},'stage':'prepared','medical_status':'pending','final_export_allowed':False}
    save(batch/'state.json',state)
    inventory=read(src/'manual-materials/material-inventory.json');item=next(x for x in inventory['items'] if x['id']=='pixabay-339323')
    source=FilesystemBackend(src)._resolve(item['path'])
    if digest(source)!=item['sha256']:raise ValueError('source stock video changed')
    directory(edit/'d20-source');image=edit/'d20-source/grapefruit-frame.png'
    tools=Toolchain(ROOT/'cache/media-tools/ffmpeg-9.0.2/bin')
    tools.ffmpeg_run(['-v','error','-i',str(source),'-ss','1.0','-frames:v','1','-y',str(image)],timeout=30)
    provenance={'source_file':str(source),'source_sha256':item['sha256'],'source_page':item['source_page'],
        'transform':{'operation':'video_frame_to_png','timestamp_seconds':1.0},'result_sha256':digest(image),
        'image_is_video_frame':True,'license_status':'unverified','source_acceptance':'pending_current_review'}
    save(edit/'d20-source/provenance.json',provenance)
    assert snapshot(original)==before
    print(encode({'state':'prepared','batch':str(batch),'target_job':identity,'image':str(image)}))


def ingest(batch):
    s=read(batch/'state.json');edit=Path(s['task'])/'edit';image=edit/'d20-source/grapefruit-frame.png'
    from media_tag.deployment.local import read_json
    root=ROOT/'data/local';instance=read_json(root/'instance.json');origin=f"http://127.0.0.1:{instance['port']}"
    with FilesystemBackend(root).open_read('owner-token.local.txt') as f:token=f.read(4096).decode().strip()
    if 'asset_id' not in s:
        provenance=read(edit/'d20-source/provenance.json');body=image.read_bytes();sha=digest(image)
        with httpx.Client(base_url=origin,headers={'Authorization':'Bearer '+token},trust_env=False,timeout=60) as client:
            def call(path,payload=None,**kwargs):
                r=client.post(path,json=payload,**kwargs);r.raise_for_status();return r.json()
            upload=call('/uploads',{'request_id':batch.name,'filename':'西柚-视频截帧-1s.png','size_bytes':len(body),'sha256':sha,
                'purpose':'library','source':{'label':'西柚（原视频第1秒截帧，仅作外观示意）','reference':provenance['source_page'],
                'attribution':'视频原件SHA256='+provenance['source_sha256']+'；FFmpeg第1.0秒截帧，结果SHA256='+sha,'license_status':'unverified'}})
            while upload['offset']<len(body):
                offset=upload['offset'];chunk=body[offset:offset+upload['chunk_bytes']]
                r=client.put(f"/uploads/{upload['upload_id']}/chunks?offset={offset}",content=chunk,headers={'X-Chunk-SHA256':sha256(chunk).hexdigest()});r.raise_for_status();upload=r.json()
            result=call('/uploads/'+upload['upload_id']+'/complete');aid=result['asset_id']
            asset=client.get('/v1/assets/'+aid);asset.raise_for_status()
            call('/ui/library/'+aid,{'request_id':batch.name+'-reader','expected_revision':asset.json()['revision'],
                'output_sha256':sha,'active':True,'recipients':[instance['reader_id']]})
            s.update(asset_id=aid,asset_sha256=sha,stage='ingested');save(batch/'state.json',s)
    from media_tag.client.http import LibraryClient,load_settings,LibraryClientError
    from media_tag.search.public import AssetSearch
    reader=LibraryClient(load_settings(root/'reader-client.local.json'));deadline=time.monotonic()+120
    while True:
        try:
            manifest=reader.manifest(s['asset_id'],s['asset_sha256'])
            if manifest['technical']['report_state']=='ready':break
        except LibraryClientError:pass
        if time.monotonic()>deadline:raise RuntimeError('technical extraction not ready')
        time.sleep(.5)
    result=reader.search(AssetSearch(query='西柚',literal_only=True,use_model=False));save(batch/'api-search.json',result)
    assert any(a['asset_id']==s['asset_id'] for a in result['items'])
    print(encode({'state':'ingested','asset_id':s['asset_id'],'sha256':s['asset_sha256'],'search_state':result['state']}))


def acquire(batch):
    s=read(batch/'state.json');task=Path(s['task']);edit=task/'edit';candidate=edit/'automation/runs/d20/candidate.json'
    from media_tag.client.models import Acquisition
    bridge=EditingSource(HOST/'config/media-tag.local.json')
    request=Acquisition(asset_id=s['asset_id'],sha256=s['asset_sha256'],context={'unit_id':'u04','quote':'先说第一种，西柚，也就是葡萄柚。',
        'purpose':'仅展示红肉西柚切面的外观，帮助对应口播中的水果名称；不证明药理作用或饮食建议。',
        'transcript_path':'edit/cut.transcript.json','candidate_path':'edit/automation/runs/d20/candidate.json',
        'transcript_sha256':digest(edit/'cut.transcript.json'),'candidate_sha256':digest(candidate),
        'timeline':{'start_seconds':21.16,'end_seconds':23.24}})
    value={'id':'d20-grapefruit','acquisition':request.model_dump(mode='json')};save(batch/'acquisition.json',value)
    result=bridge.fetch(s['target_job'],value);save(batch/'acquired.json',result)
    assert Path(result['external_path']).is_relative_to(Path('E:/asset'))
    assert digest(Path(result['external_path']))==s['asset_sha256']
    item=result['item'];item.update(notice='用药请遵医嘱',attribution='',display={'layout':'doctor-pip','fit':'contain','motion':'none',
        'doctorWindow':{'shape':'circle','rect':{'x':76,'y':1080,'width':320,'height':320},'fit':'cover','cropFocus':{'x':.5,'y':.38},'enter':'fly'}})
    props=read(candidate);props['media']=[item];save(candidate,props)
    s.update(stage='acquired',external_path=result['external_path'],host_media_path=result['host_path']);save(batch/'state.json',s)
    print(encode({'state':'acquired','candidate':str(candidate),'external':result['external_path']}))


def rebind(batch):
    s=read(batch/'state.json');edit=Path(s['task'])/'edit'
    save(edit/'prep.inputs.json',{'input_mode':'precut','video':'cut.preview.mp4','script':'../script.md','transcript':'cut.transcript.json','report':'cut.preview.report.json'})
    code="import {preparationContext} from 'file:///D:/ai/auto-video/editor/server/preparation.mjs';console.log(JSON.stringify(await preparationContext(process.argv[1],process.argv[2])));"
    env=os.environ|{'PYTHONUTF8':'1','EDITOR_PYTHON':sys.executable,'PATH':str(ROOT/'cache/media-tools/ffmpeg-9.0.2/bin')+os.pathsep+os.environ.get('PATH','')}
    result=subprocess.run(['node','--input-type=module','-e',code,str(HOST),str(edit)],capture_output=True,text=True,encoding='utf8',env=env,timeout=60,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.returncode:
        (batch/'context-error.log').write_text(result.stderr,encoding='utf8');raise RuntimeError('current task context failed')
    context=json.loads(result.stdout);save(batch/'current-context.json',context)
    plan=read(edit/'preparation.json');plan['input_hashes']=context['inputs']['hashes'];plan['style_version']=context['style']['version']
    plan['input_paths']=context['inputs']['paths'];plan['evidence_inputs']=dict(context['inputs']['paths'])
    plan['evidence_inputs']['script']=str((edit/context['inputs']['paths']['script']).resolve())
    save(edit/'preparation.json',plan)
    print(encode({'state':'input_context_rebound','input_mode':context['inputs']['input_mode']}))


def adopt(batch):
    from datetime import datetime,timezone
    s=read(batch/'state.json');edit=Path(s['task'])/'edit';run=edit/'automation/runs/d20'
    proof=read(batch/'codex/verified.json');answer=read(batch/'codex/answer.json')
    assert answer['verdict']=='pass' and proof['actual_host']=='Codex CLI'
    assert all(digest(Path(p))==h for p,h in proof['input_hashes'].items())
    shutil.copyfile(run/'candidate.json',run/'candidate-reviewed.json')
    save(run/'model-review.json',{'answer':answer,'actual_host_proof':proof,'medical_approved':False})
    shutil.copyfile(batch/'api-search.json',run/'api-search.json')
    registry=read(edit/'media_assets.json');asset=next(a for a in registry['assets'] if a['id']=='d20-grapefruit')
    item=read(run/'candidate.json')['media'][0];context=asset['media_tag']['acquisition']['context']
    observations={
      'semantic':'原图为红肉柑橘切面，限既有来源标记的西柚外观示意，不证明药理或营养判断。',
      'action':'静态截帧，无动作演示或动态方向主张。',
      'timing':'使用窗口21.16-23.24秒，按原词级时间与25fps量化，随后返回医生。',
      'composition':'实际535/555及退场590帧可见等比水果图、完整医生小窗，不裁切必要文本。',
      'packaging':'已实际检查完整字幕、用药请遵医嘱和底部压字可见，未改医生命题或顺序。',
      'technical':'实际下载SHA256/尺寸/格式及解码通过，静图没有音轨；未据此声称全片试听通过。',
      'style':'使用现有reference-v2模板，只有该短窗口替换，其他字幕/风格保持用户选定输入。',
      'source':'所选任务的原下载文件/来源页、源SHA256、截帧时间与结果SHA256可追溯；当前来源网页访问失败已保留，未声称重新核实作者。',
      'license':'原项目许可检查开关为false，本项skipped，许可保持未核实。',
      'eye_mosaic':'原静帧实际未见人物，现有本地隐私处理实际扫描1帧；医生背景按原规则保留。'}
    record={'asset_id':asset['id'],'unit_id':context['unit_id'],'quote':context['quote'],'purpose':context['purpose'],'relation':'direct',
      'stage':'composite','decision':'pass','reviewer_type':'model','reviewed_by':'agent:codex-d20-native',
      'reviewed_at':datetime.now(timezone.utc).isoformat(),'actual_media_observed':True,
      'source_file':item['src'],'source_sha256':asset['sha256'],'start':item['start'],'end':item['end'],
      'transcript_sha256':context['transcript_sha256'],'candidate_sha256':digest(run/'candidate.json'),
      'checks':{k:{'status':'skipped' if k=='license' else 'not_applicable' if k=='action' else 'pass','observation':v} for k,v in observations.items()}}
    save(run/'material-acceptance.json',{'records':[record]})
    command(batch,'accept_materials.py',s['target_job'],'--candidate','automation/runs/d20/candidate.json','--acceptance','automation/runs/d20/material-acceptance.json')
    result=subprocess.run(['node',str(ROOT/'tests/integration/bind_real_plan.mjs'),str(batch)],capture_output=True,text=True,encoding='utf8',timeout=30,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (batch/'bind-plan.log').write_text(result.stdout+result.stderr,encoding='utf8');assert result.returncode==0
    s['stage']='current_still_use_accepted';save(batch/'state.json',s)
    print(encode({'state':s['stage'],'medical_approved':False,'audio_listened':False,'continuous_video_viewed':False}))


def review(batch):
    s=read(batch/'state.json');edit=Path(s['task'])/'edit';run=edit/'automation/runs/d20';folder=batch/'codex';directory(folder)
    images=[Path(s['host_media_path']),*[run/'checks'/f'still-{f}.png' for f in (520,535,555,590)]]
    bindings={str(p):digest(p) for p in [run/'candidate.json',*images]};save(folder/'input-hashes.json',bindings)
    schema={'type':'object','additionalProperties':False,'properties':{
        'verdict':{'type':'string','enum':['pass','reject']},'visible_subject':{'type':'string'},'observations':{'type':'array','items':{'type':'string'}},
        **{key:{'type':'boolean'} for key in ('source_has_people','source_has_medical_labels','caption_readable','doctor_face_visible','notice_visible','medical_approved','continuous_video_viewed','audio_listened')}},
        'required':['verdict','visible_subject','observations','source_has_people','source_has_medical_labels','caption_readable','doctor_face_visible','notice_visible','medical_approved','continuous_video_viewed','audio_listened']}
    save(folder/'schema.json',schema)
    prompt=f'''This is the actual D20 read-only integration of media_tag into the selected existing editing project.
Do not edit files, run shell commands, create agents, infer a patient's identity, or approve medical claims.
Use the actual globally configured media_tag MCP tools in order:
search_assets query="西柚", literal_only=true, use_model=false; get_asset asset_id="{s['asset_id']}";
get_asset_preview asset_id="{s['asset_id']}", sha256="{s['asset_sha256']}";
get_asset_download for that same fixed version.
The first attached image is the actual downloaded/hashed still PNG from E:/asset and the editing privacy processor.
It is explicitly a 1.0-second frame extraction from the existing stock video, not a generated photo or video playback.
Remaining attachments are actual unadopted Remotion candidate frames: before, during entry, display, and after exit.
The exact current utterance is “先说第一种，西柚，也就是葡萄柚。”; window 21.16-23.24 seconds in a 171.64-second video.
Purpose: identify the pictured fruit's appearance only. It does not prove any drug interaction, nutrition advice or diagnosis.
Judge this narrowly scoped still-image use from the actual pixels: recognizable cut fruit, no people in the source,
doctor face visible in the composited circular window, no covering of complete captions or disclaimer, and 用药请遵医嘱 visible during display.
The shot exits before the following comparison. Do not claim continuous motion or audio review; those fields must be false.
Medical approval must remain false. Return pass only if the actual static composition supports this limited use, else reject.
Describe visible observations, not filenames or match scores. No fabricated tool calls or permission/medical approvals.'''
    executable=shutil.which('codex');assert executable,'installed Codex CLI required'
    args=[executable,'exec','--ephemeral','--json','--color','never','--sandbox','read-only','--cd',str(ROOT),
        '-c','notify=[]','-c','mcp_servers.node_repl.enabled=false','-c','mcp_servers.media_tag.required=true',
        '--output-schema',str(folder/'schema.json'),'--output-last-message',str(folder/'answer.json')]
    for p in images:args.extend(['--image',str(p)])
    args.append('-')
    with (folder/'events.jsonl').open('w',encoding='utf8') as events,(folder/'stderr.log').open('w',encoding='utf8') as errors:
        result=subprocess.run(args,input=prompt,text=True,encoding='utf8',stdout=events,stderr=errors,timeout=300,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    assert result.returncode==0,'actual Codex host failed; inspect private diagnostics'
    answer=read(folder/'answer.json')
    assert answer['verdict']=='pass' and answer['caption_readable'] and answer['doctor_face_visible'] and answer['notice_visible']
    assert not any(answer[k] for k in ('source_has_people','source_has_medical_labels','medical_approved','continuous_video_viewed','audio_listened'))
    stream=[json.loads(line) for line in (folder/'events.jsonl').read_text(encoding='utf8').splitlines() if line.startswith('{')]
    calls=[v['item'] for v in stream if v.get('type')=='item.completed' and v.get('item',{}).get('type')=='mcp_tool_call' and v['item'].get('server')=='media_tag']
    assert {c['tool'] for c in calls}=={'search_assets','get_asset','get_asset_preview','get_asset_download'}
    search=next(c['result']['structured_content'] for c in calls if c['tool']=='search_assets')
    from media_tag.client.http import LibraryClient,load_settings
    from media_tag.search.public import AssetSearch
    direct=LibraryClient(load_settings(ROOT/'data/local/reader-client.local.json')).search(AssetSearch(query='西柚',literal_only=True,use_model=False))
    def stable(value):return value|{'resolution':{k:v for k,v in value['resolution'].items() if k not in ('cache_hit','elapsed_seconds')}}
    assert stable(search)==stable(direct),'API/MCP mismatch'
    assert all(digest(Path(p))==h for p,h in bindings.items())
    save(folder/'verified.json',{'actual_host':'Codex CLI','mcp_calls':len(calls),'api_mcp_equal':True,'input_hashes':bindings,
        'medical_approved':False,'audio_listened':False,'continuous_video_viewed':False})
    s.update(stage='source_and_composition_reviewed');save(batch/'state.json',s)
    print(encode({'state':'reviewed','actual_mcp_calls':len(calls),'observations':answer['observations']}))


def access_checks(batch):
    """Real fixed-version interruption/resume, cached revocation and restoration."""
    from urllib.error import URLError
    from media_tag.client.provider import TaskProvider
    from media_tag.client.models import Acquisition
    from media_tag.client.http import LibraryClientError
    from media_tag.search.public import AssetSearch
    from media_tag.deployment.local import read_json
    s=read(batch/'state.json');bridge=EditingSource(HOST/'config/media-tag.local.json')
    spec=Acquisition.model_validate(read(batch/'acquisition.json')['acquisition'])
    job='d20-retry-'+uuid4().hex[:12]
    provider=TaskProvider(bridge.client,bridge.assets/'jobs',job,bridge.tools)
    opener=bridge.client._opener
    class InterruptedResponse:
        def __init__(self,response):self.response=response;self.read_once=False
        def __getattr__(self,name):return getattr(self.response,name)
        def __enter__(self):self.response.__enter__();return self
        def __exit__(self,*args):return self.response.__exit__(*args)
        def read1(self,size):
            if self.read_once:raise URLError('D20 deliberate transfer interruption')
            self.read_once=True;return self.response.read1(min(size,32768))
    class InterruptOnce:
        def open(self,request,**kwargs):
            response=opener.open(request,**kwargs)
            return InterruptedResponse(response) if '/content?' in request.full_url else response
    try:
        bridge.client._opener=InterruptOnce()
        try:provider.fetch(spec)
        except LibraryClientError as exc:assert exc.code=='download_interrupted'
        else:raise AssertionError('deliberate interruption did not occur')
    finally:bridge.client._opener=opener
    part=provider.store.path(spec.sha256+'.part');offset=part.stat().st_size
    assert 0<offset<read(batch/'acquired.json').get('size_bytes',Path(s['external_path']).stat().st_size)
    resumed=provider.fetch(spec);assert resumed['resumed_bytes']==offset
    assert digest(Path(resumed['local_path']))==s['asset_sha256']
    assert provider.fetch(spec)['reused'] is True
    root=ROOT/'data/local';instance=read_json(root/'instance.json')
    with FilesystemBackend(root).open_read('owner-token.local.txt') as stream:token=stream.read(4096).decode().strip()
    edit=Path(s['task'])/'edit';props=read(edit/'edit.json');registry=read(edit/'media_assets.json')
    value={'item':props['media'][0],'asset':registry['assets'][0]}
    def refresh_access_revision():
        # Enrollment/withdrawal increments revision. Refresh only a revision-only
        # receipt change after real access/hash/provenance checks; never ignore a
        # technical, source, transform, file or use-context change.
        binding=value['asset']['media_tag'];original=bridge.provider(s['target_job'],spec)
        previous=original.store.read_json('receipt',binding['receipt_sha256'])
        current=original.fetch(spec)
        assert {k:v for k,v in previous.items() if k!='revision'}=={k:v for k,v in current['receipt'].items() if k!='revision'},'non-access source metadata changed'
        binding['receipt_sha256']=current['receipt_sha256'];save(edit/'media_assets.json',registry)
        return {'before':previous['revision'],'after':current['receipt']['revision'],'new_receipt_sha256':current['receipt_sha256']}
    initial_refresh=refresh_access_revision()
    assert bridge.validate(s['target_job'],value)['state']=='valid'
    with httpx.Client(base_url=bridge.client.origin,headers={'Authorization':'Bearer '+token},trust_env=False,timeout=30) as client:
        def grant(recipients,suffix):
            current=client.get('/v1/assets/'+s['asset_id']);current.raise_for_status()
            response=client.post('/ui/library/'+s['asset_id'],json={'request_id':job+'-'+suffix,
                'expected_revision':current.json()['revision'],'output_sha256':s['asset_sha256'],'active':True,'recipients':recipients})
            response.raise_for_status()
        denied=[]
        try:
            grant([],'withdraw')
            for name,check in [('cached_provider',lambda:provider.fetch(spec)),('editing_source',lambda:bridge.validate(s['target_job'],value))]:
                try:check()
                except LibraryClientError as exc:
                    assert exc.status in (403,404);denied.append({'entry':name,'code':exc.code,'status':exc.status})
                else:raise AssertionError('withdrawn cached material remained available')
        finally:grant([instance['reader_id']],'restore')
    refreshed=refresh_access_revision()
    assert bridge.validate(s['target_job'],value)['state']=='valid'
    no_match=bridge.client.search(AssetSearch(query='d20-no-such-fruit-'+uuid4().hex,literal_only=True,use_model=False))
    assert no_match['items']==[]
    result={'state':'passed','deliberate_transfer_interruption':True,'actual_resumed_bytes':offset,
        'download_sha256':digest(Path(resumed['local_path'])),'cached_reuse':True,'withdrawal_denials':denied,
        'reader_access_restored':True,'empty_search_confirmed':True,'retry_job':job,
        'initial_revision_refresh':initial_refresh,'restored_revision_refresh':refreshed,'medical_approved':False}
    save(batch/'access-checks.json',result);print(encode(result))


def finish(batch):
    """Check actual output and immutable inputs; never equate decode with watching."""
    s=read(batch/'state.json');was_finished=s['stage']=='local_real_preview_verified'
    edit=Path(s['task'])/'edit';run=edit/'automation/runs/d20'
    proof=read(batch/'codex/verified.json')
    for name,expected in proof['input_hashes'].items():
        actual=run/'candidate-reviewed.json' if Path(name)==run/'candidate.json' else Path(name)
        assert digest(actual)==expected,'review input changed'
    props=read(edit/'edit.json');assert props==read(run/'candidate.json')
    assert read(batch/'editor-apply.json')['ok'] is True
    assert read(batch/'access-checks.json')['reader_access_restored'] is True
    original=HOST/'jobs'/s['source_task'];before=read(batch/'source-snapshot.json')
    assert snapshot(original)==before,'original selected task changed'
    assert all(digest(HOST/p)==h for p,h in s['policy_hashes'].items()),'preview/export policy changed'
    for key in ('source','segments','captions','bgm','sfx','soundEffects','outro'):
        assert props.get(key)==read(original/'edit/edit.json').get(key),key+' changed'
    for name in ('cut.preview.mp4','cut.preview.report.json','cut.transcript.json','audio.processing.json'):
        assert digest(edit/name)==before['edit/'+name],name+' changed'
    assert digest(Path(s['external_path']))==s['asset_sha256']==digest(Path(s['host_media_path']))
    tools=ROOT/'cache/media-tools/ffmpeg-9.0.2/bin';video=edit/'preview.mp4'
    result=subprocess.run([str(tools/'ffprobe.exe'),'-v','error','-show_streams','-show_format','-of','json',str(video)],capture_output=True,encoding='utf8',timeout=30)
    assert result.returncode==0;info=json.loads(result.stdout);save(batch/'preview-probe.json',info)
    v=next(x for x in info['streams'] if x['codec_type']=='video');a=next(x for x in info['streams'] if x['codec_type']=='audio')
    assert (v['width'],v['height'],v['avg_frame_rate'],int(v['nb_frames']))==(720,1280,'25/1',4291)
    assert abs(float(info['format']['duration'])-171.64)<.2
    assert abs(float(v['duration'])-float(a['duration']))<.2
    decoded=subprocess.run([str(tools/'ffmpeg.exe'),'-v','error','-xerror','-i',str(video),'-map','0:v:0','-map','0:a:0','-f','null','-'],capture_output=True,encoding='utf8',timeout=120)
    (batch/'full-decode.log').write_text(decoded.stderr,encoding='utf8');assert decoded.returncode==0
    pixels=batch/'final-pixels';directory(pixels)
    extracted=subprocess.run([str(tools/'ffmpeg.exe'),'-v','error','-i',str(video),'-vf',r'select=eq(n\,535)+eq(n\,555)',
        '-fps_mode','vfr','-frames:v','2','-y',str(pixels/'frame-%d.png')],capture_output=True,encoding='utf8',timeout=45)
    assert extracted.returncode==0
    similarities={}
    for number,frame in enumerate((535,555),start=1):
        compared=subprocess.run([str(tools/'ffmpeg.exe'),'-i',str(pixels/f'frame-{number}.png'),'-i',str(run/'checks'/f'still-{frame}.png'),
            '-lavfi','ssim','-f','null','-'],capture_output=True,encoding='utf8',timeout=15)
        assert compared.returncode==0
        match=re.search(r'All:([0-9.]+)',compared.stderr);assert match
        similarities[str(frame)]=float(match[1]);assert similarities[str(frame)]>.95,'final preview differs from actual reviewed frame'
    save(batch/'final-pixel-comparison.json',{'method':'FFmpeg SSIM against the actually reviewed frame; not continuous viewing',
        'values':similarities,'minimum':.95,'additional_images_sent_to_codex':0})
    env=os.environ|{'PYTHONUTF8':'1','PATH':str(tools)+os.pathsep+os.environ.get('PATH','')}
    blocked=subprocess.run([sys.executable,str(HOST/'scripts/render_job.py'),s['target_job']],env=env,capture_output=True,encoding='utf8',timeout=30)
    (batch/'final-export-guard.log').write_text(blocked.stdout+blocked.stderr,encoding='utf8')
    assert blocked.returncode!=0 and '正式导出已暂停' in blocked.stderr+blocked.stdout
    assert not (edit/'final.mp4').exists() and not (edit/'authorizations/medical.json').exists()
    report={'schema_version':'d20-real-result-v1','state':'local_real_preview_verified','source_task':s['source_task'],
        'target_job':s['target_job'],'python':sys.executable,'preview':str(video),'preview_sha256':digest(video),
        'preview_duration_seconds':float(info['format']['duration']),'frames':4291,'whole_video_audio_decode_passed':True,
        'reviewed_to_final_pixel_ssim':similarities,'qa_diagnostics':read(edit/'qa.json')['flags'],
        'original_task_files_unchanged':len(before),'policy_hashes_unchanged':True,'api_mcp_equal':proof['api_mcp_equal'],
        'actual_codex_mcp_calls':proof['mcp_calls'],'actual_codex_reviewed_images':5,'access_checks':read(batch/'access-checks.json'),
        'final_export_blocked':True,'medical_approved':False,'license_status':'unverified','continuous_video_viewed':False,'audio_listened':False,
        'semantic_business_acceptance':'pending_formal_vocabulary_and_independent_set','host_baseline_tests':'3 existing failures also reproduced on unmodified HEAD',
        'scope':'same Windows computer; one static fruit use; no cross-machine/OSS/medical/full AV acceptance'}
    save(batch/'validation.json',report);s['stage']=report['state'];save(batch/'state.json',s)
    if not was_finished:command(batch,'stage_clock.py','end',s['target_job'],'package')
    print(encode({k:report[k] for k in ('state','preview','preview_sha256','frames','original_task_files_unchanged','final_export_blocked')}))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('phase',choices=('prepare','ingest','acquire','review','rebind','adopt','access','finish'))
    p.add_argument('--source-job');p.add_argument('--batch',type=Path);p.add_argument('--codex-images-approved',action='store_true')
    args=p.parse_args()
    if args.phase=='prepare':prepare(args.source_job)
    elif args.phase=='ingest':ingest(args.batch.resolve())
    elif args.phase=='acquire':acquire(args.batch.resolve())
    elif args.phase=='review':
        if not args.codex_images_approved:raise ValueError('explicit permission for the five images is required')
        review(args.batch.resolve())
    elif args.phase=='rebind':rebind(args.batch.resolve())
    elif args.phase=='access':access_checks(args.batch.resolve())
    elif args.phase=='finish':finish(args.batch.resolve())
    else:adopt(args.batch.resolve())


if __name__=='__main__':main()
