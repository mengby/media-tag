"""Actual E: acquisition + adjacent editing registration/privacy/acceptance/validation, synthetic only."""
from datetime import datetime,timezone
from hashlib import sha256
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from search.validate_public import main as execute
from media_tag.catalog.service import encode
from media_tag.client.editing import EditingSource,read
from media_tag.client.models import Acquisition


def extension(env):
    host=Path('D:/ai/auto-video');config=host/'config/media-tag.local.json';original=read(config)
    settings=json.loads(original);folder=env.folder/'editing';folder.mkdir()
    (folder/'README.md').write_text('Actual configured E:/asset and D:/ai/auto-video engineering validation. Synthetic geometry/check attestations only; no real AI viewing, medicine or final export.\n')
    token=folder/'reader-token.local.txt';token.write_text(env.tokens['reader'])
    client=folder/'client.local.json';client.write_text(encode({'base_url':env.url,'token_file':str(token)}))
    settings['client_config']=str(client)
    policy={p:sha256((host/p).read_bytes()).hexdigest() for p in ('config/workflow-policy.json','config/media-sources.json','scripts/render_job.py','scripts/check_edit.py')}
    identity='d19-engineering-'+uuid4().hex[:8];commands=[];errors=[]
    def run(script,*args):
        result=subprocess.run([sys.executable,str(host/'scripts'/script),*args],capture_output=True,text=True,timeout=600,
            creationflags=subprocess.CREATE_NO_WINDOW)
        commands.append({'script':script,'exit_code':result.returncode})
        if result.returncode:
            (folder/'failed.stderr.log').write_text(result.stderr,encoding='utf8')
            raise AssertionError('actual editing command failed: '+script)
        assert all(t not in result.stdout+result.stderr for t in env.tokens.values())
        return result.stdout
    def grant(asset,recipients,request):
        current=env.router.catalog.get_asset(asset.asset_id,principal=env.people['owner'])
        r=httpx.post(env.url+'/ui/library/'+asset.asset_id,headers={'Authorization':'Bearer '+env.tokens['owner']},
            json={'request_id':request,'expected_revision':current.revision,'output_sha256':current.sha256,'active':True,'recipients':recipients},timeout=30)
        assert r.status_code==200
    try:
        config.write_text(encode(settings),encoding='utf8');source=EditingSource(config)
        run('media_tag_source.py','init-job',identity)
        task=source.task(identity);edit=task/'edit';run_dir=edit/'automation/runs/test';run_dir.mkdir(parents=True)
        for path in (edit/'automation',edit/'automation/runs',run_dir):
            (path/'README.md').write_text('Synthetic integration check only. No actual viewing approval.\n')
        (edit/'cut.transcript.json').write_text('{"synthetic":"geometry only","words":[]}')
        candidate={'segments':[{'from':0,'to':6}],'outro':{'duration':1},'cards':[],'media':[]}
        candidate_path=run_dir/'candidate.json';candidate_path.write_text(encode(candidate))
        query=folder/'query.json';query.write_text(encode({'query':env.picture_asset.asset_id,'literal_only':True,'use_model':False}))
        search=json.loads(run('media_tag_source.py','search',identity,'--input',str(query)))
        assert search['items'] and (task/search['search_record']).exists()
        grant(env.derived_asset,[env.people['reader'].actor_id],'d19-derived-reader')
        fetched=[]
        for local_id,asset,start,end,interval in (
            ('shape',env.picture_asset,1.,2.,None),('motion',env.derived_asset,3.,3.5,{'start_seconds':0.,'end_seconds':.5})):
            spec=Acquisition(asset_id=asset.asset_id,sha256=asset.sha256,context={'unit_id':'unit-'+local_id,
                'quote':'Synthetic geometry','purpose':'Engineering source integration only',
                'transcript_sha256':sha256((edit/'cut.transcript.json').read_bytes()).hexdigest(),
                'candidate_sha256':sha256(candidate_path.read_bytes()).hexdigest(),
                'candidate_path':'edit/automation/runs/test/candidate.json','timeline':{'start_seconds':start,'end_seconds':end},'source_interval':interval})
            value=folder/(local_id+'-fetch.json');value.write_text(encode({'id':local_id,'acquisition':spec.model_dump(mode='json')}))
            result=json.loads(run('media_tag_source.py','fetch',identity,'--input',str(value)))
            repeated=json.loads(run('media_tag_source.py','fetch',identity,'--input',str(value)))
            assert repeated['reused'] and repeated['host_path']==result['host_path'] and repeated['external_path']==result['external_path']
            assert Path(result['external_path']).is_relative_to(Path('E:/asset'))
            assert sha256(Path(result['external_path']).read_bytes()).hexdigest()==asset.sha256
            candidate['media'].append(result['item']);candidate_path.write_text(encode(candidate));fetched.append(result)
        registry=json.loads((edit/'media_assets.json').read_text())
        assert all(a['provider']=='media-tag' and a['approval']['status']=='candidate' and a['eye_mosaic']['status']=='processed' for a in registry['assets'])
        acceptance=[]
        for item in candidate['media']:
            a=next(a for a in registry['assets'] if a['id']==item['id']);c=a['media_tag']['acquisition']['context']
            r={'asset_id':a['id'],'stage':'composite','decision':'pass','reviewer_type':'model','reviewed_by':'agent:synthetic-contract-test',
               'reviewed_at':datetime.now(timezone.utc).isoformat(),'actual_media_observed':True,'relation':'illustrates',
               'unit_id':c['unit_id'],'quote':c['quote'],'purpose':c['purpose'],'source_file':item['src'],'source_sha256':a['sha256'],
               'transcript_sha256':c['transcript_sha256'],'candidate_sha256':sha256(candidate_path.read_bytes()).hexdigest(),
               'start':item['start'],'end':item['end'],'checks':{k:{'status':'skipped' if k=='license' else 'pass',
                 'observation':'Synthetic contract fixture; not an actual AI/business approval.'} for k in ('semantic','action','timing','composition','packaging','technical','style','source','license','eye_mosaic')}}
            if item['type']=='video':r.update(source_start=item['sourceStart'],source_end=item['sourceStart']+item['end']-item['start'])
            acceptance.append(r)
        accept_path=run_dir/'material-acceptance.json';accept_path.write_text(encode({'records':acceptance}))
        run('accept_materials.py',identity,'--candidate','automation/runs/test/candidate.json','--acceptance','automation/runs/test/material-acceptance.json')
        sys.path.insert(0,str(host/'scripts'));validator=importlib.import_module('media_validation')
        accepted=json.loads(candidate_path.read_text());registry=json.loads((edit/'media_assets.json').read_text())
        assert not any(m.get('candidate') for m in accepted['media'])
        failures,warnings=validator.validate_media(accepted,edit,6,30);assert not failures,failures
        original_hash=sha256(Path(fetched[0]['external_path']).read_bytes()).hexdigest()
        # Current file changes, failed actual viewing attestation and source withdrawal all fail closed.
        bad={**acceptance[0],'actual_media_observed':False}
        try:importlib.import_module('media_tag_source').validate_acceptance(accepted['media'][0],registry['assets'][0],bad)
        except ValueError:errors.append('unviewed_rejected')
        else:raise AssertionError('unviewed check passed')
        grant(env.derived_asset,[],'d19-withdraw-derived')
        failures,_=validator.validate_media(accepted,edit,6,30)
        assert any('media-tag' in error for error in failures);errors.append('copied_source_revocation_rejected')
        denied=httpx.get(env.url+'/v1/assets/'+env.video_asset.asset_id,headers={'Authorization':'Bearer '+env.tokens['reader']},timeout=20)
        assert denied.status_code==403
        assert sha256(Path(fetched[0]['external_path']).read_bytes()).hexdigest()==original_hash
        assert all(sha256((host/p).read_bytes()).hexdigest()==value for p,value in policy.items())
        result={'state':'passed','actual_host_root':str(host),'actual_asset_root':'E:/asset','new_job_id':identity,'commands':commands,
            'actual_host_privacy_processing':True,'image_and_retimed_video':True,'whole_preview_validator_passed':True,
            'retime_applied_again':False,'failure_checks':errors,'policy_hashes_unchanged':policy,
            'actual_ai_viewing':False,'synthetic_acceptance_only':True,'medical_status':'pending','final_export_performed':False,
            'external_downloads':[{'sha256':sha256(Path(r['external_path']).read_bytes()).hexdigest(),'path':r['external_path']} for r in fetched]}
        (folder/'validation.json').write_text(encode(result));return result
    finally:
        config.write_bytes(original)
        assert sha256(config.read_bytes()).digest()==sha256(original).digest()


if __name__=='__main__':raise SystemExit(execute(extension=extension,prefix='d19-editing'))
