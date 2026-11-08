"""Real CPU BGE/HTTP/CLI diagnostics using synthetic geometry, never a business dataset."""
from hashlib import sha256
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from terminology.lexicon.validate_http import server,FINGERPRINT
from terminology.lexicon.conftest import publish,taxonomy,option,concept,alias,lexicon
from media_tag.api.config import load_application
from media_tag.catalog import Principal
from media_tag.catalog.service import encode
from media_tag.contracts.models import FieldValue
from media_tag.review.models import Observation,FieldDecision
from media_tag.router.runtime import LocalExecutor,CPUHandlers
from media_tag.search.models import ConceptLink
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.terminology.contracts import Lexicon,Rollback
from media_tag.terminology.evaluation.models import Dataset,Pin
from media_tag.terminology.evaluation.runner import Client,pool
from media_tag.terminology.evaluation.metrics import score
from media_tag.terminology.resolve.models import IndexActivation
from media_tag.web.models import Enrollment


def main():
    root=Path.cwd();folder=root/'jobs'/('d16-validation-'+uuid4().hex[:8]);folder.mkdir()
    (folder/'README.md').write_text('Private real D16 tool diagnostics, synthetic geometry and synthetic annotations only. No business acceptance.\n')
    started=time.monotonic();summary={'business_acceptance':False,'synthetic_engineering_only':True,'interpreter':sys.executable}
    try:
        dirs={}
        for name in ('workspace','objects','originals'):
            path=folder/name;path.mkdir();(path/'README.md').write_text('Private generated D16 engineering data.\n');dirs[name]=path
        owner=Principal(actor_id='d16-synthetic-engineering',role='editor');worker=Principal(actor_id='d16-worker',role='worker')
        token=secrets.token_urlsafe(32);worktoken=secrets.token_urlsafe(32)
        config=dict(workspace=str(dirs['workspace']),database=str(folder/'catalog.sqlite3'),object_store=str(dirs['objects']),
            template_dir=str(root/'asset-index-template'),template_fingerprint=FINGERPRINT,workers=[worker.actor_id],web_enabled=True,
            credentials={sha256(token.encode()).hexdigest():owner.model_dump(mode='json'),sha256(worktoken.encode()).hexdigest():worker.model_dump(mode='json')},
            review={'enabled':True},terminology={'enabled':True,'managers':[owner.actor_id],'publishers':[owner.actor_id]},
            text={'enabled':True,'model_enabled':True,'model_root':str(root/'cache/models/d14'),'cache_entries':0},search={'enabled':True})
        cfg=folder/'validation.local.json';cfg.write_text(encode(config),encoding='utf8');app,router,tools=load_application(cfg)
        request_count=0
        @app.middleware('http')
        async def count_requests(request,call_next):
            nonlocal request_count
            request_count+=1
            return await call_next(request)
        env=SimpleNamespace(service=app.state.terminology,owner=owner,manager=owner,publisher=owner)
        tax=publish(env,taxonomy(option('purpose',field='用途大类',value='信息图解')),'synthetic-tax')
        payload=lexicon(tax['release_id'],concepts=[concept('circle',oid='purpose',name='合成圆形',concept_type='other',definition='红色圆形的合成工程图形轮廓',scope='只用于几何工程检索'),
            concept('square',oid='purpose',name='合成方形',concept_type='other',definition='蓝色方形的合成工程图形轮廓',scope='只用于几何工程检索')],
            aliases=[alias('round',expression='合成圆圈',target_id='circle'),alias('poly-a',expression='几何形状说法',target_id='circle',default_expand=False,ambiguity_group='geometry'),
                alias('poly-b',expression='几何形状说法',target_id='square',default_expand=False,ambiguity_group='geometry')])
        words=publish(env,payload,'synthetic-words')
        activated=app.state.resolver.indices.activate(IndexActivation(request_id='activate',expected_generation=2,expected_serving_epoch=0,
            taxonomy_release_id=tax['release_id'],lexicon_release_id=words['release_id'],note='Synthetic D16 frozen tuple'),principal=owner)
        image=Image.new('RGB',(96,64),'white');ImageDraw.Draw(image).ellipse((20,4,76,60),fill='red');source=dirs['originals']/'circle.png';image.save(source)
        asset=router.catalog.register_file(FilesystemBackend(dirs['originals']),source.name,source_backend_id='d16-generated',principal=owner,request_id='source',name='Synthetic engineering frame')
        app.state.search.library.enroll(asset.asset_id,Enrollment(request_id='enroll',expected_revision=1,output_sha256=asset.sha256,active=True),principal=owner)
        executor=LocalExecutor(router,worker,CPUHandlers(router,worker,tools));attempts=[]
        while (result:=executor.run_once()) is not None:attempts.append(result);assert result['accepted']
        asset=router.catalog.get_asset(asset.asset_id,principal=owner)
        obs=app.state.search.reviews.observe(asset.asset_id,Observation(request_id='view',expected_revision=asset.revision,source_sha256=asset.sha256,kind='image',viewed=True,
            note='Engineering code opened the generated red-circle PNG; no medical assertion'),principal=owner)
        app.state.search.reviews.decide(asset.asset_id,FieldDecision(request_id='subject',expected_revision=asset.revision,source_sha256=asset.sha256,field='画面主体',
            value=FieldValue(state='observed',values=('合成圆形',)),observation_ids=(obs['observation_id'],),note='Synthetic geometry only'),principal=owner)
        asset=router.catalog.get_asset(asset.asset_id,principal=owner)
        app.state.search.links.create(asset.asset_id,ConceptLink(request_id='geometry-link',expected_revision=asset.revision,source_sha256=asset.sha256,lexicon_release_id=words['release_id'],
            concept_id='circle',observation_ids=(obs['observation_id'],),note='Synthetic nonmedical geometry only'),principal=owner)
        before=router.catalog.get_asset(asset.asset_id,principal=owner)
        def case(cid,query,category,expectation,known,auto,related=(),**extra):
            return dict(case_id=cid,source_group='synthetic-source-'+cid,query=query,category=category,expectation=expectation,
                acceptable_concepts=known,allowed_automatic_concepts=auto,relevant_asset_ids=related,annotation_note='Synthetic protocol expectation, not an independent business annotation',**extra)
        with server(app) as url:
            client=Client(url,token);digest,_=pool(client);versions=client.call('/search/vocabulary')['versions']
            pin=Pin(**{key:versions[key] for key in Pin.model_fields})
            base=dict(dataset_id='synthetic-d16',split='calibration',purpose='synthetic_engineering',frozen_at='2026-11-08T00:00:00Z',
                annotators=['codex-synthetic-fixture-author'],adjudicator='engineering-protocol-only',annotation_reference='Generated geometry protocol expectations only',
                independently_human_annotated=False,pin=pin.model_dump(mode='json'),candidate_pool_sha256=digest,
                cases=[case('cal-circle','红色的圆盘状图形','common','clear',['circle'],['circle'],[asset.asset_id]),case('cal-out','航班座位购票','out_of_scope','reject',[],[])])
            calibration=Dataset.model_validate_json(encode(base));calpath=folder/'calibration.json';calpath.write_text(calibration.model_dump_json(),encoding='utf8')
            evaluation=Dataset.model_validate_json(encode(base|dict(dataset_id='synthetic-evaluation',split='evaluation',calibration_sha256=sha256(calpath.read_bytes()).hexdigest(),
                cases=[case('common','合成圆圈','common','clear',['circle'],['circle'],[asset.asset_id]),
                    case('poly','几何形状说法','ambiguous','clarify',['circle','square'],[]),
                    case('negative','没有合成圆圈','challenge','clear',['circle'],['circle'],expected_polarity='negated',forbidden_asset_ids=[asset.asset_id]),
                    case('out','量子计算机量子比特','out_of_scope','reject',[],[])])))
            evalpath=folder/'evaluation.json';evalpath.write_text(evaluation.model_dump_json(),encoding='utf8');tokenpath=folder/'token.local.txt';tokenpath.write_text(token,encoding='utf8')
            with router.database.transaction() as c:
                bundle=json.loads(c.execute('SELECT bundle_json FROM terminology_text_indices WHERE index_id=?',(activated['index_id'],)).fetchone()[0])
            checksums=folder/'index-checksums.json';checksums.write_text(encode(bundle['files']),encoding='utf8')
            def run(args):
                p=subprocess.run([sys.executable,str(root/'scripts/evaluate_terminology.py'),*args,'--jobs-root',str(folder)],capture_output=True,timeout=120)
                if p.returncode:raise AssertionError(p.stderr.decode('utf8',errors='replace'))
                return json.loads(p.stdout)
            frozen=run(['--snapshot','--base-url',url,'--token-file',str(tokenpath)])['scope']
            assert json.loads(Path(frozen).read_bytes())['candidate_pool_sha256']==digest
            calibration_report=run(['--dataset',str(calpath),'--calibrate-index',str(dirs['workspace']/bundle['directory']),'--bundle-file',str(checksums),'--model-root',str(root/'cache/models/d14')])['report']
            report=run(['--dataset',str(evalpath),'--calibration',str(calpath),'--base-url',url,'--token-file',str(tokenpath)])['report']
            measured=json.loads(Path(report).read_bytes());assert not measured['business_acceptance'] and not measured['business_eligible']
            assert measured['metrics']['lexicon']['asset_hit10']['value']==1 and measured['metrics']['lexicon_bge']['forbidden_asset_hits']==0
            options=app.state.resolver.options
            try:
                app.state.resolver.options=options.model_copy(update={'query_timeout_seconds':.000001})
                timed=client.call('/search/query',{'request':{'query':'从未索引过的工程短句一'}})
                assert timed['resolution']['state']=='degraded' and timed['resolution']['model_state']=='timed_out'
                assert score(evaluation.cases[-1],timed,elapsed_seconds=.01)['failure']=='text_model_timeout'
                assert client.call('/search/query',{'request':{'query':'合成圆圈'}})['resolution']['state']=='resolved'
                app.state.resolver.options=options.model_copy(update={'model_enabled':False})
                offline=client.call('/search/query',{'request':{'query':'从未索引过的工程短句二'}})
                assert offline['resolution']['state']=='degraded' and offline['resolution']['reason']=='text_model_disabled'
            finally:app.state.resolver.options=options
            # Real publication and paired rollback, retaining the old immutable index.
            new=payload.model_dump(mode='json');new['aliases'].append(alias('after',expression='后续圆圈说法',target_id='circle'))
            released=publish(env,Lexicon.model_validate_json(encode(new)),'synthetic-after',words['release_id'])
            app.state.resolver.indices.activate(IndexActivation(request_id='activate-after',expected_generation=3,expected_serving_epoch=1,
                taxonomy_release_id=tax['release_id'],lexicon_release_id=released['release_id'],note='Synthetic changed vocabulary'),principal=owner)
            assert client.call('/search/query',{'request':{'query':'后续圆圈说法','use_model':False}})['selected_concept_ids']==['circle']
            env.service.rollback(Rollback(request_id='rollback',expected_generation=3,taxonomy_release_id=tax['release_id'],lexicon_release_id=words['release_id'],note='Synthetic D16 paired rollback'),principal=owner)
            rollback=app.state.resolver.indices.activate(IndexActivation(request_id='activate-rollback',expected_generation=4,expected_serving_epoch=2,
                taxonomy_release_id=tax['release_id'],lexicon_release_id=words['release_id'],note='Restore exact frozen tuple'),principal=owner)
            assert rollback['reused'] and rollback['index_id']==activated['index_id']
            assert not client.call('/search/query',{'request':{'query':'后续圆圈说法','use_model':False}})['selected_concept_ids']
        assert router.catalog.get_asset(asset.asset_id,principal=owner).revision==before.revision
        assert sha256(source.read_bytes()).hexdigest()==asset.sha256
        from media_tag.contracts.templates import load_template_bundle
        assert load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT).fingerprint==FINGERPRINT
        with router.database.transaction() as c:
            assert c.execute("SELECT count(*) FROM effective_values WHERE field IN ('科室','器官/系统','疾病/症状','过程/状态')").fetchone()[0]==0
            assert c.execute('SELECT count(*) FROM index_confirmations').fetchone()[0]==0
        summary.update(state='passed',comparison_report=report,calibration_report=calibration_report,scope_snapshot=frozen,real_bge_cpu=True,actual_cpu_attempts=len(attempts),
            paired_rollback_reused_original_index=True,source_hash_and_revision_unchanged=True,medical_effective_values_written=0,
            timeout_offline_and_no_match_distinguished=True,exact_fallback_survived_timeout=True,
            index_confirmations_created=0,privacy_approvals_created=0,http_requests=request_count,elapsed_seconds=time.monotonic()-started)
        (folder/'validation.json').write_text(encode(summary),encoding='utf8');print(json.dumps({'state':'passed','report':str(folder/'validation.json')}));return 0
    except Exception as exc:
        (folder/'failure.json').write_text(encode(summary|{'state':'failed','error':type(exc).__name__}),encoding='utf8');raise


if __name__=='__main__':raise SystemExit(main())
