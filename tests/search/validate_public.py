"""D17 real CPU/HTTP retrieval and fixed-byte acquisition; synthetic engineering only."""
from hashlib import sha256
import json
from pathlib import Path
import secrets
import sys
import time
from types import SimpleNamespace
from uuid import uuid4
from PIL import Image,ImageDraw
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from terminology.lexicon.validate_http import server,FINGERPRINT
from terminology.lexicon.conftest import publish,taxonomy,option,concept,alias,lexicon
from media_tag.api.config import load_application,executor_from_config
from media_tag.catalog import Principal
from media_tag.catalog.service import encode
from media_tag.contracts.models import FieldValue
from media_tag.contracts.templates import load_template_bundle
from media_tag.review.models import Observation,FieldDecision
from media_tag.search.models import ConceptLink
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.terminology.resolve.models import IndexActivation
from media_tag.web.models import Enrollment


def main():
    root=Path.cwd();folder=root/'jobs'/('d17-semantic-'+uuid4().hex[:8]);folder.mkdir()
    (folder/'README.md').write_text('Private D17 synthetic geometry retrieval/retime/byte acquisition diagnostics. No medical or privacy approval.\n')
    started=time.monotonic();report={'synthetic_engineering_only':True,'business_acceptance':False,'interpreter':sys.executable}
    try:
        dirs={}
        for name in ('workspace','objects','originals','downloads'):
            path=folder/name;path.mkdir();(path/'README.md').write_text('Private generated D17 engineering artifacts.\n');dirs[name]=path
        people={n:Principal(actor_id='d17-engineering-'+n,role='worker' if n=='worker' else 'reader' if n in ('reader','derived') else 'editor') for n in ('owner','reader','derived','outsider','worker')}
        tokens={n:secrets.token_urlsafe(32) for n in people}
        config=dict(workspace=str(dirs['workspace']),database=str(folder/'catalog.sqlite3'),object_store=str(dirs['objects']),
            template_dir=str(root/'asset-index-template'),template_fingerprint=FINGERPRINT,workers=[people['worker'].actor_id],web_enabled=True,
            credentials={sha256(tokens[n].encode()).hexdigest():p.model_dump(mode='json') for n,p in people.items()},review={'enabled':True},retime={'enabled':True},
            terminology={'enabled':True,'managers':[people['owner'].actor_id],'publishers':[people['owner'].actor_id]},
            text={'enabled':True,'model_enabled':True,'model_root':str(root/'cache/models/d14')},search={'enabled':True})
        cfg=folder/'validation.local.json';cfg.write_text(encode(config),encoding='utf8');app,router,tools=load_application(cfg);owner=people['owner']
        env=SimpleNamespace(service=app.state.terminology,owner=owner,manager=owner,publisher=owner)
        tax=publish(env,taxonomy(option('purpose',field='用途大类',value='信息图解')),'synthetic-tax')
        words=publish(env,lexicon(tax['release_id'],concepts=[concept('circle',oid='purpose',name='合成圆形',concept_type='other',definition='红色圆形的合成工程图形轮廓',scope='仅工程几何'),
            concept('square',oid='purpose',name='合成方形',concept_type='other',definition='蓝色方形的合成工程图形轮廓',scope='仅工程几何')],
            aliases=[alias('round',expression='合成圆圈',target_id='circle'),alias('poly-a',expression='几何形状说法',target_id='circle',default_expand=False,ambiguity_group='geometry'),
                alias('poly-b',expression='几何形状说法',target_id='square',default_expand=False,ambiguity_group='geometry')]),'synthetic-words')
        app.state.resolver.indices.activate(IndexActivation(request_id='activate',expected_generation=2,expected_serving_epoch=0,
            taxonomy_release_id=tax['release_id'],lexicon_release_id=words['release_id'],note='Synthetic D17 tuple'),principal=owner)
        picture=dirs['originals']/'circle.png';image=Image.new('RGB',(96,64),'white');ImageDraw.Draw(image).ellipse((20,4,76,60),fill='red');image.save(picture)
        for number in range(8):
            image=Image.new('RGB',(96,64),'white');r=4+number*3;ImageDraw.Draw(image).ellipse((48-r,32-r,48+r,32+r),fill='red');image.save(dirs['originals']/f'grow-{number:02d}.png')
        video=dirs['originals']/'growth.mp4';tools.ffmpeg_run(['-v','error','-framerate','8','-i',str(dirs['originals']/'grow-%02d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(video)],timeout=20)
        original_hashes={p.name:sha256(p.read_bytes()).hexdigest() for p in (picture,video)}
        assets=[]
        for number,path in enumerate((picture,video)):
            asset=router.catalog.register_file(FilesystemBackend(dirs['originals']),path.name,source_backend_id='generated-d17',principal=owner,request_id='source-'+str(number),name='Engineering source '+str(number))
            app.state.library.enroll(asset.asset_id,Enrollment(request_id='enroll-'+str(number),expected_revision=asset.revision,output_sha256=asset.sha256,active=True,
                recipients=(people['reader'].actor_id,)),principal=owner);assets.append(asset)
        hidden=router.catalog.register_file(FilesystemBackend(dirs['originals']),picture.name,source_backend_id='hidden-d17',principal=people['outsider'],request_id='private',name='合成圆形 private must not leak')
        app.state.library.enroll(hidden.asset_id,Enrollment(request_id='private-enroll',expected_revision=hidden.revision,output_sha256=hidden.sha256,active=True),principal=people['outsider'])
        executor=executor_from_config(cfg,people['worker'].actor_id);outcomes=[]
        def drain():
            while (value:=executor.run_once()) is not None:outcomes.append(value);assert value['accepted']
        drain()
        for number,asset in enumerate(assets):
            asset=router.catalog.get_asset(asset.asset_id,principal=owner)
            obs=app.state.search.reviews.observe(asset.asset_id,Observation(request_id='view-'+str(number),expected_revision=asset.revision,source_sha256=asset.sha256,
                kind='image' if number==0 else 'video_segment',start_seconds=None if number==0 else .125,end_seconds=None if number==0 else .75,viewed=True,
                note='Synthetic geometry protocol observation, not a business human or medical review'),principal=owner)
            if number==0:
                app.state.search.reviews.decide(asset.asset_id,FieldDecision(request_id='purpose',expected_revision=asset.revision,source_sha256=asset.sha256,
                    field='用途大类',value=FieldValue(state='observed',values=('信息图解',)),observation_ids=(obs['observation_id'],),note='Synthetic information diagram'),principal=owner)
                asset=router.catalog.get_asset(asset.asset_id,principal=owner)
            app.state.search.links.create(asset.asset_id,ConceptLink(request_id='link-'+str(number),expected_revision=asset.revision,source_sha256=asset.sha256,
                lexicon_release_id=words['release_id'],concept_id='circle',direction='unspecified' if number==0 else 'increasing',
                observation_ids=(obs['observation_id'],),note='Synthetic circle semantics and actual generated growth interval only'),principal=owner)
        count=0
        with server(app) as url,httpx.Client(base_url=url,timeout=30) as client:
            def call(method,path,payload=None,*,actor='reader',status=200,headers=None,raw=False):
                nonlocal count
                count+=1;response=client.request(method,path,json=payload,headers={'Authorization':'Bearer '+tokens[actor],**(headers or {})})
                assert response.status_code==status,(path,response.status_code,response.text[:500])
                assert 'no-store' in response.headers['cache-control']
                return response if raw else response.json()
            assert call('GET','/v1/capabilities')['search_enabled'];call('GET','/v1/search-contract')
            first=call('POST','/v1/assets/search',{'query':'合成圆圈','limit':1});assert first['next_cursor'] and len(first['items'])==1
            second=call('POST','/v1/assets/search',{'query':'合成圆圈','limit':1,'cursor':first['next_cursor']});assert first['items'][0]['asset_id']!=second['items'][0]['asset_id']
            assert hidden.name not in encode(first)+encode(second)
            call('POST','/v1/assets/search',{'query':'合成圆圈','limit':1,'cursor':first['next_cursor']},actor='outsider',status=409)
            call('GET','/v1/assets/'+hidden.asset_id,status=403)
            ambiguous=call('POST','/v1/assets/search',{'query':'几何形状说法'});assert not ambiguous['selected_concept_ids']
            chosen=call('POST','/v1/assets/search',{'query':'几何形状说法','selected_concept_ids':['circle'],'selection_lexicon_release_id':words['release_id']});assert len(chosen['items'])==2
            purpose=call('POST','/v1/assets/search',{'query':'合成圆圈','filters':{'usages':['信息图解'],'format_names':['png_pipe'],'min_display_width':96}});assert len(purpose['items'])==1
            assert not call('POST','/v1/assets/search',{'query':'合成圆圈','filters':{'require_redacted':True}})['items']
            direction=call('POST','/v1/assets/search',{'query':'合成圆圈','context':{'direction':'increasing'},'selected_concept_ids':['circle'],
                'selection_lexicon_release_id':words['release_id'],'filters':{'media_kinds':['video'],'min_file_duration_seconds':.9,'max_file_duration_seconds':1.1}})
            assert len(direction['items'])==1;selected=direction['items'][0];fragment=selected['fragments'][0]
            assert fragment['start_seconds']==.125 and fragment['end_seconds']==.75 and selected['technical']['frame_rate']==8
            preview=call('GET',selected['preview_manifest']+'&start_seconds=0.125&end_seconds=0.75')
            assert not preview['requested_playback']['server_clip_generated'] and not preview['full_video_observed']
            native=next(r for r in preview['resources'] if r['kind']=='source_media');call('HEAD',native['content'],raw=True)
            call('GET',native['content'],headers={'Range':'bytes=0-127'},status=206,raw=True)
            manifest=call('GET',selected['download_manifest']);size=manifest['size_bytes'];split=size//2
            one=call('GET',manifest['content'],headers={'Range':f'bytes=0-{split-1}'},status=206,raw=True).content
            two=call('GET',manifest['content'],headers={'Range':f'bytes={split}-'},status=206,raw=True).content
            assert sha256(one+two).hexdigest()==selected['sha256']
            downloaded=dirs['downloads']/'retrieved.mp4';downloaded.write_bytes(one+two)
            tools.ffmpeg_run(['-v','error','-xerror','-i',str(downloaded),'-f','null','-'],timeout=20)
            call('GET',selected['download_manifest'].replace(selected['sha256'],'f'*64),status=412)
            # Real retiming creates new bytes; public API never reapplies the transform.
            source_asset=router.catalog.get_asset(assets[1].asset_id,principal=owner)
            run=call('POST','/retime/jobs',{'request_id':'two-speed','parent_asset_id':source_asset.asset_id,'expected_revision':source_asset.revision,'speed':2.0},actor='owner')
            drain();rendered=call('GET','/retime/jobs/'+run['job_id'],actor='owner');assert rendered['asset_id'] and rendered['cumulative_speed']==2
            derived=router.catalog.get_asset(rendered['asset_id'],principal=owner)
            call('POST','/ui/library/'+derived.asset_id,{'request_id':'derived-enroll','expected_revision':derived.revision,'output_sha256':derived.sha256,'active':True,
                'recipients':[people['derived'].actor_id]},actor='owner');drain()
            deriv=call('POST','/v1/assets/search',{'query':derived.asset_id,'literal_only':True},actor='derived')['items'][0]
            assert deriv['transform']['cumulative_speed']==2 and not deriv['transform']['apply_transform_again']
            dmanifest=call('GET',deriv['download_manifest'],actor='derived');data=call('GET',dmanifest['content'],actor='derived',raw=True).content
            assert sha256(data).hexdigest()==derived.sha256 and derived.sha256!=source_asset.sha256
            call('GET','/v1/assets/'+source_asset.asset_id+'/download-manifest?sha256='+source_asset.sha256,actor='derived',status=403)
            # Revoking one source grant invalidates its old manifest/cursor; no original fallback.
            current=router.catalog.get_asset(source_asset.asset_id,principal=owner)
            call('POST','/ui/library/'+source_asset.asset_id,{'request_id':'revoke-reader','expected_revision':current.revision,'output_sha256':current.sha256,'active':True,'recipients':[]},actor='owner')
            call('GET',manifest['content'],status=403)
            call('POST','/v1/assets/search',{'query':'合成圆圈','limit':1,'cursor':first['next_cursor']},status=409)
            assert len(call('POST','/v1/assets/search',{'query':'合成圆圈'})['items'])==1
            (folder/'responses.json').write_text(encode({'first':first,'direction':direction,'preview':preview,'download':manifest,'derived':deriv}),encoding='utf8')
        assert all(sha256(p.read_bytes()).hexdigest()==original_hashes[p.name] for p in (picture,video))
        assert load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT).fingerprint==FINGERPRINT
        with router.database.transaction() as c:
            counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('index_confirmations','privacy_reviews')}
            assert counts=={'index_confirmations':0,'privacy_reviews':0}
            assert c.execute("SELECT count(*) FROM effective_values WHERE field IN ('科室','器官/系统','疾病/症状','过程/状态')").fetchone()[0]==0
        report.update(state='passed',http_requests=count,actual_cpu_attempts=len(outcomes),real_bge_cpu=True,source_hashes_unchanged=True,
            explicit_source_acl_revision_change=True,download_sha256=selected['sha256'],derived_sha256=derived.sha256,
            whole_file_decode_passed=True,bounded_playback_manifest=True,paired_versioned_search=True,
            derived_only_grant_did_not_grant_source=True,old_manifest_and_cursor_revocation_passed=True,
            medical_effective_values_written=0,privacy_approvals_created=0,index_confirmations_created=0,elapsed_seconds=time.monotonic()-started)
        (folder/'validation.json').write_text(encode(report),encoding='utf8');print(json.dumps({'state':'passed','report':str(folder/'validation.json')}));return 0
    except Exception as exc:
        (folder/'failure.json').write_text(encode(report|{'state':'failed','error':type(exc).__name__}),encoding='utf8');raise


if __name__=='__main__':raise SystemExit(main())
