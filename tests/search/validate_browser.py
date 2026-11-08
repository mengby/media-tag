"""Isolated D15 engineering validation: real browser, source bytes and BGE, no medical acceptance."""
from hashlib import sha256
import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from uuid import uuid4
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from terminology.lexicon.validate_http import server,FINGERPRINT
from terminology.lexicon.conftest import option,taxonomy,concept,alias,lexicon,publish
from search.test_imports import xlsx
from types import SimpleNamespace
from media_tag.api.config import load_application
from media_tag.catalog import Principal
from media_tag.router.runtime import LocalExecutor,CPUHandlers
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.web.service import LibraryService
from media_tag.web.models import Enrollment
from media_tag.terminology.resolve.models import IndexActivation


def main():
    root=Path.cwd();job=root/'jobs'/('d15-validation-20261108-'+uuid4().hex[:8]);job.mkdir()
    (job/'README.md').write_text('Private D15 synthetic geometry + real browser/CPU BGE validation. No clinical vocabulary, physician identity or medical acceptance.\n',encoding='utf-8')
    started=time.monotonic();stop=threading.Event();worker=None
    report={'job_id':job.name,'interpreter':sys.executable,'synthetic_engineering_only':True,'medical_acceptance':False}
    try:
        folders={}
        for name in ('workspace','objects','originals','browser'):
            folders[name]=job/name;folders[name].mkdir();(folders[name]/'README.md').write_text('Private D15 generated engineering artifacts.\n')
        picture=folders['originals']/'synthetic-circle.png';image=Image.new('RGB',(96,64),'white');ImageDraw.Draw(image).ellipse((20,4,76,60),fill='red');image.save(picture)
        for number in range(8):
            frame=Image.new('RGB',(96,64),'white');radius=24-number*2;ImageDraw.Draw(frame).ellipse((48-radius,32-radius,48+radius,32+radius),fill='red');frame.save(folders['originals']/f'shrink-{number:03d}.png')
        people={name:Principal(actor_id='d15-engineering-'+name,role=role) for name,role in [('owner','editor'),('manager','editor'),('outsider','editor'),('reader','reader'),('worker','worker')]}
        tokens={name:secrets.token_urlsafe(32) for name in people}
        config={'workspace':str(folders['workspace']),'database':str(job/'synthetic.sqlite3'),'object_store':str(folders['objects']),
            'template_dir':str(root/'asset-index-template'),'template_fingerprint':FINGERPRINT,
            'credentials':{sha256(tokens[n].encode()).hexdigest():p.model_dump(mode='json') for n,p in people.items()},'workers':[people['worker'].actor_id],
            'web_enabled':True,'review':{'enabled':True},'terminology':{'enabled':True,'managers':[people['manager'].actor_id],'publishers':[people['manager'].actor_id]},
            'text':{'enabled':True,'model_enabled':True,'model_root':str(root/'cache/models/d14'),'query_timeout_seconds':10.0,'build_timeout_seconds':60.0},'search':{'enabled':True}}
        configpath=job/'validation.local.json';configpath.write_text(json.dumps(config),encoding='utf-8')
        app,router,tools=load_application(configpath);catalog=router.catalog
        video=folders['originals']/'shrinking-circle.mp4'
        generated=subprocess.run([str(tools.ffmpeg),'-v','error','-n','-framerate','8','-i',str(folders['originals']/'shrink-%03d.png'),'-c:v','libx264','-pix_fmt','yuv420p',str(video)],capture_output=True,timeout=30)
        assert generated.returncode==0
        env=SimpleNamespace(service=app.state.terminology,owner=people['manager'],manager=people['manager'],publisher=people['manager'])
        tax=publish(env,taxonomy(option('purpose',field='用途大类',value='信息图解')),'synthetic-tax-v1')
        words=lexicon(tax['release_id'],concepts=[concept('circle',oid='purpose',name='合成圆形',concept_type='other',definition='合成工程中的红色圆形轮廓',scope='仅用于工程几何检索，不作医学解释'),
            concept('square',oid='purpose',name='合成方形',concept_type='other',definition='合成工程中的方形轮廓',scope='仅用于工程几何检索')],
            aliases=[alias('round',expression='合成圆圈',target_id='circle')])
        lex=publish(env,words,'synthetic-lex-v1')
        app.state.resolver.indices.activate(IndexActivation(request_id='initial-index',expected_generation=2,expected_serving_epoch=0,
            taxonomy_release_id=tax['release_id'],lexicon_release_id=lex['release_id'],note='Synthetic geometry serving tuple'),principal=people['manager'])
        private=catalog.register_file(FilesystemBackend(folders['originals']),picture.name,source_backend_id='generated-synthetic',principal=people['outsider'],request_id='private',name='禁止返回的合成圆形')
        LibraryService(router).enroll(private.asset_id,Enrollment(request_id='private-library',expected_revision=private.revision,output_sha256=private.sha256,active=True),principal=people['outsider'])
        before={p.name:sha256(p.read_bytes()).hexdigest() for p in (picture,video)}
        executor=LocalExecutor(router,people['worker'],CPUHandlers(router,people['worker'],tools));outcomes=[]
        def run():
            while not stop.is_set():
                value=executor.run_once()
                if value is not None:outcomes.append(value)
                stop.wait(.05)
        worker=threading.Thread(target=run,name='d15-real-cpu-worker',daemon=True);worker.start()
        workbook=folders['originals']/'candidates.xlsx';workbook.write_bytes(xlsx([['说法','专业名称'],['另一个圆圈','合成圆形'],['目标待补说法','不确定']]))
        with server(app) as url:
            context={'url':url,'owner_token':tokens['owner'],'manager_token':tokens['manager'],'manager_id':people['manager'].actor_id,
                'private_name':private.name,'image':str(picture),'video':str(video),'xlsx':str(workbook),'output':str(folders['browser']),
                'chrome':'C:/Program Files/Google/Chrome/Application/chrome.exe',
                'playwright':'C:/Users/EDY/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright'}
            contextpath=job/'browser.local.json';contextpath.write_text(json.dumps(context),encoding='utf-8')
            process=subprocess.run(['node',str(root/'tests/search/browser-check.mjs'),str(contextpath)],capture_output=True,timeout=240)
            (job/'browser.stdout.log').write_bytes(process.stdout);(job/'browser.stderr.log').write_bytes(process.stderr)
            assert process.returncode==0,process.stderr.decode('utf-8',errors='replace')
        stop.set();worker.join(10);assert not worker.is_alive()
        browser=json.loads((folders['browser']/'result.json').read_text())
        with router.database.transaction() as c:
            counts={t:c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('assets','review_observations','review_decisions','asset_concept_links','terminology_feedback','terminology_releases','terminology_text_indices','catalog_gaps','index_confirmations')}
            medical=c.execute("SELECT count(*) FROM effective_values WHERE field IN ('科室','器官/系统','疾病/症状','过程/状态')").fetchone()[0]
            revisions=[dict(r) for r in c.execute('SELECT asset_id,revision FROM assets ORDER BY asset_id')]
            tasks=[dict(r) for r in c.execute('SELECT state,spec_json FROM router_tasks WHERE active=1')]
        assert medical==0 and counts['index_confirmations']==0
        assert all(v['accepted'] for v in outcomes) and all(t['state']=='succeeded' for t in tasks)
        assert all(sha256(p.read_bytes()).hexdigest()==before[p.name] for p in (picture,video))
        from media_tag.contracts.templates import load_template_bundle
        assert load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT).fingerprint==FINGERPRINT
        report.update(state='passed',browser=browser,counts=counts,medical_effective_values=medical,index_confirmations_created=0,privacy_approvals_created=0,
            actual_cpu_attempts=len(outcomes),source_hashes_unchanged=True,asset_revisions=revisions,template_fingerprint=FINGERPRINT,
            real_bge_model_revision=app.state.resolver.registry.revision,elapsed_seconds=time.monotonic()-started)
        (job/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'state':'passed','report':str(job/'validation.json')}));return 0
    except Exception as exc:
        report.update(state='failed',error=type(exc).__name__,elapsed_seconds=time.monotonic()-started)
        (job/'failure.json').write_text(json.dumps(report,indent=2),encoding='utf-8');raise
    finally:
        stop.set()
        if worker:worker.join(10)


if __name__=='__main__':raise SystemExit(main())
