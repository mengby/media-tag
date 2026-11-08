"""Fresh real HTTP/CPU BGE + task-local byte acquisition. Synthetic protocol, no AI host."""
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from search.validate_public import main as execute
from media_tag.catalog.service import encode
from media_tag.client.http import ClientSettings,LibraryClient,LibraryClientError
from media_tag.client.models import Acquisition,CurrentUseCheck
from media_tag.client.provider import TaskProvider
from media_tag.web.models import Enrollment


class InterruptedResponse:
    def __init__(self,response):self.response=response;self.remaining=int(response.headers['Content-Length'])//2;self.status=response.status;self.headers=response.headers
    def __enter__(self):return self
    def __exit__(self,*args):self.response.close()
    def read(self,n=-1):return self.read1(n)
    def read1(self,n=-1):
        if self.remaining<=0:return b''
        chunk=self.response.read1(min(n,self.remaining));self.remaining-=len(chunk);return chunk


def extension(env):
    folder=env.folder/'client';folder.mkdir();(folder/'README.md').write_text('D19 fresh HTTP client diagnostics; synthetic engineering only. No actual AI host or business current-use review.\n')
    jobs=folder/'jobs';jobs.mkdir();(jobs/'README.md').write_text('New isolated editing tasks, not adjacent projects or shared assets.\n')
    clients={};configs={}
    for actor in ('reader','derived'):
        token=folder/(actor+'-token.local.txt');token.write_text(env.tokens[actor])
        settings=ClientSettings(base_url=env.url,token_file=str(token));clients[actor]=LibraryClient(settings)
        configs[actor]=folder/(actor+'.local.json');configs[actor].write_text(encode(settings.model_dump(mode='json')),encoding='utf8')
    def spec_for(provider,asset,*,video=False):
        transcript=b'{"synthetic":"geometry task only"}';candidate=b'{"synthetic":"current candidate"}'
        (provider.store.task/'edit/cut.transcript.json').write_bytes(transcript)
        (provider.store.task/'edit/candidate.json').write_bytes(candidate)
        return Acquisition(asset_id=asset.asset_id,sha256=asset.sha256,context={'unit_id':'geometry','quote':'Synthetic geometry','purpose':'Show geometric shape',
            'transcript_sha256':sha256(transcript).hexdigest(),'candidate_sha256':sha256(candidate).hexdigest(),
            'timeline':{'start_seconds':0.0,'end_seconds':0.5 if video else 1.0},
            'source_interval':{'start_seconds':0.0,'end_seconds':0.5} if video else None})
    provider=TaskProvider(clients['derived'],jobs,'derived-task',env.tools)
    spec=spec_for(provider,env.derived_asset,video=True)
    original=clients['derived']._opener
    class Once:
        def __init__(self):self.pending=True;self.range_seen=None
        def open(self,request,**kwargs):
            response=original.open(request,**kwargs)
            if '/content?' in request.full_url:
                if self.pending:self.pending=False;return InterruptedResponse(response)
                self.range_seen=request.get_header('Range')
            return response
    interrupted=Once();clients['derived']._opener=interrupted
    try:provider.fetch(spec);raise AssertionError('interruption must fail')
    except LibraryClientError as exc:assert exc.code=='download_incomplete' and exc.retryable
    result=provider.fetch(spec);assert result['resumed_bytes']>0 and interrupted.range_seen
    assert result['receipt']['transform']['cumulative_speed']==2.0 and not result['receipt']['apply_transform_again']
    assert result['receipt']['technical']['duration_seconds']==.5
    assert provider.fetch(spec)['receipt_sha256']==result['receipt_sha256']
    try:
        provider.fetch(spec.model_copy(update={'asset_id':env.video_asset.asset_id,'sha256':env.video_asset.sha256}));raise AssertionError('derived scope cannot retrieve original')
    except LibraryClientError as exc:assert exc.status==403
    # The real CLI uses the same service and creates a separate task, never adopts into a host.
    request=folder/'acquisition.json';request.write_text(encode(spec.model_dump(mode='json')),encoding='utf8')
    cli=subprocess.run([sys.executable,'-m','media_tag','acquire-media','--config',str(configs['derived']),'--jobs-root',str(jobs),'--job-id','cli-task','--input',str(request)],
        capture_output=True,timeout=60,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert cli.returncode==0 and json.loads(cli.stdout)['receipt']['sha256']==spec.sha256
    assert all(t.encode() not in cli.stdout+cli.stderr for t in env.tokens.values())
    picture=TaskProvider(clients['reader'],jobs,'picture-task',env.tools,license_validation=False)
    picture_spec=spec_for(picture,env.picture_asset);picture_result=picture.fetch(picture_spec)
    check={'receipt_sha256':picture_result['receipt_sha256'],'context':picture_spec.context.model_dump(mode='json'),'source_sha256':picture_spec.sha256,
        'decision':'pass','reviewer_type':'model','reviewed_by':'synthetic-protocol-fixture','reviewed_at':datetime.now(timezone.utc).isoformat(),'actual_media_observed':True,
        'evidence':[{'path':picture_result['receipt']['src'],'sha256':picture_spec.sha256}],
        'checks':{k:{'status':'skipped' if k=='license' else 'pass','observation':'Synthetic protocol attestation fixture. Not a real AI host review.'}
            for k in ('semantic','action','timing','composition','packaging','technical','style','source','license','eye_mosaic')}}
    parsed=CurrentUseCheck.model_validate_json(encode(check));handoff=picture.prepare_preview(parsed)
    assert handoff['handoff']['preview_only'] and not handoff['handoff']['final_export_allowed'] and handoff['handoff']['medical_status']=='pending'
    current=env.router.catalog.get_asset(env.picture_asset.asset_id,principal=env.people['owner'])
    from media_tag.web.service import LibraryService
    LibraryService(env.router).enroll(current.asset_id,Enrollment(request_id='d19-revoke-reader',expected_revision=current.revision,output_sha256=current.sha256,
        active=True),principal=env.people['owner'])
    try:picture.prepare_preview(parsed);raise AssertionError('old handoff cannot bypass revocation')
    except LibraryClientError as exc:assert exc.status==403
    return {'provider':'media-tag','real_cli_download':True,'real_range_resume':interrupted.range_seen,'resumed_bytes':result['resumed_bytes'],
        'receipt_sha256':result['receipt_sha256'],'real_download_sha256':spec.sha256,'derived_speed':2.0,'speed_not_reapplied':True,
        'source_denied_to_derived_reader':True,'cached_reuse_revocation_rejected':True,'current_use_protocol_only':True,'actual_media_observed_by_ai':False,
        'actual_ai_host_validated':False,'adjacent_project_written':False,'preview_only_preserved':True,'medical_status':'pending','business_acceptance':False}


if __name__=='__main__':raise SystemExit(execute(extension=extension,prefix='d19-provider'))
