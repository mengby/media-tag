"""Real CPU BGE + loopback + CLI validation, using only a private synthetic lexicon."""
from hashlib import sha256
import json
from pathlib import Path
import secrets
import statistics
import subprocess
import sys
import time
from uuid import uuid4

import httpx
from PIL import Image,ImageDraw

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from terminology.lexicon.validate_http import server,FINGERPRINT
from media_tag.api.config import load_application
from media_tag.catalog import Catalog,Database,Principal
from media_tag.contracts.templates import load_template_bundle
from media_tag.router.models import JobSpec,StepSpec
from media_tag.contracts.models import Versions,TaskLimits
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.terminology.embedding.registry import BGERegistry
from media_tag.terminology.embedding.process import ProcessEncoder
from media_tag.terminology.resolve.models import ResolveInput,ResolveTask,PREPROCESS_VERSION


def main():
    root=Path.cwd();job=root/'jobs'/('d14-validation-20261108-'+uuid4().hex[:8]);job.mkdir()
    (job/'README.md').write_text('Private D14 real CPU BGE validation with synthetic geometry words. No official medical vocabulary or acceptance.\n',encoding='utf-8')
    started=time.monotonic();report={'job_id':job.name,'interpreter':sys.executable,'synthetic_vocabulary':True,'medical_acceptance':False}
    try:
        folders={}
        for name in ('workspace','objects','originals'):
            folders[name]=job/name;folders[name].mkdir();(folders[name]/'README.md').write_text('Private D14 synthetic validation input/artifacts.\n')
        source=folders['originals']/'synthetic.png';image=Image.new('RGB',(96,64),'white');ImageDraw.Draw(image).ellipse((12,8,70,60),fill='red');image.save(source)
        source_hash=sha256(source.read_bytes()).hexdigest()
        contract=load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT)
        principals={n:Principal(actor_id='d14-validation-'+n,role=role) for n,role in [('owner','editor'),('manager','editor'),('publisher','editor'),('reader','reader'),('worker','worker')]}
        tokens={n:secrets.token_urlsafe(32) for n in principals}
        config={'workspace':str(folders['workspace']),'database':str(job/'synthetic.sqlite3'),'object_store':str(folders['objects']),
            'template_dir':str(root/'asset-index-template'),'template_fingerprint':FINGERPRINT,
            'credentials':{sha256(tokens[n].encode()).hexdigest():p.model_dump(mode='json') for n,p in principals.items()},'workers':[principals['worker'].actor_id],
            'terminology':{'enabled':True,'managers':[principals['manager'].actor_id],'publishers':[principals['publisher'].actor_id]},
            'text':{'enabled':True,'model_enabled':True,'model_root':str(root/'cache/models/d14'),'query_timeout_seconds':10.0,'build_timeout_seconds':60.0}}
        config_path=job/'validation.local.json';config_path.write_text(json.dumps(config),encoding='utf-8')
        catalog=Catalog(Database(job/'synthetic.sqlite3'),FilesystemBackend(folders['objects'],writable=True),contract=contract)
        asset=catalog.register_file(FilesystemBackend(folders['originals']),'synthetic.png',source_backend_id='d14-synthetic',principal=principals['owner'],request_id='source',name='Synthetic red circle')
        app,router,_=load_application(config_path);resolver=app.state.resolver;registry=resolver.registry
        source_record={'kind':'synthetic','locator':job.name+' geometry input','quote':'Synthetic circle and square, not medical concepts','usage':'Local engineering validation only'}
        def option(eid,value):return {'entry_id':eid,'field':'疾病/症状','value':value,'sources':[source_record],'rationale':'Abstract engineering fixture only'}
        taxpayload={'template_fingerprint':FINGERPRINT,'options':[option('circle-option','合成圆形-仅工程测试'),option('square-option','合成方形-仅工程测试')]}
        calls=0
        def call(client,method,path,data=None,actor='owner',status=200):
            nonlocal calls
            response=client.request(method,path,json=data,headers={'Authorization':'Bearer '+tokens[actor]});calls+=1
            assert response.status_code==status,(path,response.status_code,response.text)
            if path.startswith('/terminology/'):assert 'no-store' in response.headers['cache-control']
            return response.json()
        def publish(client,kind,payload,version,base=None):
            candidate=call(client,'POST','/terminology/candidates',{'request_id':'create-'+version,'kind':kind,'base_release_id':base,'payload':payload,'note':'Synthetic engineering fixture'})
            path='/terminology/candidates/'+candidate['candidate_id']
            call(client,'POST',path+'/review',{'request_id':'submit-'+version,'expected_revision':1,'action':'submit','note':'Synthetic submission'})
            groups=('options',) if kind=='taxonomy' else ('concepts','aliases','relations')
            ids=[entry['entry_id'] for group in groups for entry in payload.get(group,[])]
            approved=call(client,'POST',path+'/review',{'request_id':'review-'+version,'expected_revision':2,'action':'approve','entry_ids':ids,'note':'Explicit synthetic entry review, no medical approval'},actor='manager')
            generation=call(client,'GET','/terminology/active')['generation']
            return call(client,'POST',path+'/publish',{'request_id':'publish-'+version,'expected_revision':approved['revision'],'expected_generation':generation,'version':version,'note':'Synthetic fixture release only'},actor='publisher')
        def activate(client,tax,lex,generation,epoch,rid):
            return call(client,'POST','/terminology/text-indices/activate',{'request_id':rid,'expected_generation':generation,'expected_serving_epoch':epoch,
                'taxonomy_release_id':tax['release_id'],'lexicon_release_id':lex['release_id'],'note':'Synthetic vocabulary/vector tuple activation'},actor='publisher')
        with server(app) as url,httpx.Client(base_url=url,timeout=90) as client:
            tax=publish(client,'taxonomy',taxpayload,'synthetic-tax-v1')
            concepts=[{'entry_id':eid,'name':name,'concept_type':'other','definition':definition,'scope':'仅用于合成几何图形工程验证',
                'option_ids':[oid],'sources':[source_record],'rationale':'Synthetic abstract geometry'} for eid,name,definition,oid in [
                ('circle','合成红色圆形','红色圆形，圆形的轮廓，没有棱角。','circle-option'),
                ('square','合成蓝色方形','蓝色方形，四条边和四个直角。','square-option')]]
            aliases=[{'entry_id':'alias-circle','expression':'合成圆圈','target_id':'circle','relation':'equivalent','default_expand':True,'sources':[source_record],'rationale':'Synthetic exact alias'}]
            lexpayload={'template_fingerprint':FINGERPRINT,'taxonomy_release_id':tax['release_id'],'concepts':concepts,'aliases':aliases,'relations':[]}
            lex=publish(client,'lexicon',lexpayload,'synthetic-lex-v1')
            first=activate(client,tax,lex,2,0,'activate-first')
            cold_stats=dict(resolver.encoder.stats)
            exact=call(client,'POST','/terminology/resolve',{'query':'合成圆圈'})
            negative=call(client,'POST','/terminology/resolve',{'query':'没有合成红色圆形'})
            assert exact['state']=='resolved' and negative['state']=='ambiguous' and negative['candidates'][0]['intent']['polarity']=='negated'
            measurements=[];observed=[]
            texts=['红色圆圈','红色圆环','圆圈呈现红色','蓝色正方形','蓝色四边形','今天计划去买菜','窗外天气晴朗','不存在的完全陌生表达']
            for text in texts:
                before=time.monotonic();result=call(client,'POST','/terminology/resolve',{'query':text});measurements.append(time.monotonic()-before)
                assert result['model_state']=='used' and result['state'] in ('ambiguous','no_match')
                assert all(not c['default_expand'] for c in result['candidates'])
                observed.append({'query':text,'state':result['state'],'candidates':result['candidates'],'elapsed_seconds':measurements[-1]})
            cached=call(client,'POST','/terminology/resolve',{'query':texts[0]})
            assert cached['cache_hit']
            isolated=call(client,'POST','/terminology/resolve',{'query':texts[0]},actor='reader')
            assert not isolated['cache_hit']
            original_encoder=resolver.encoder
            faulty=ProcessEncoder(BGERegistry(job/'missing-model'),folders['workspace'])
            resolver.encoder=faulty
            degraded=call(client,'POST','/terminology/resolve',{'query':'真实缺失模型演练'})
            assert degraded['state']=='degraded' and 'text_model_missing' in degraded['errors']
            assert call(client,'POST','/terminology/resolve',{'query':'合成圆圈'})['state']=='resolved'
            resolver.encoder=original_encoder;faulty.close()
            lex2=publish(client,'lexicon',lexpayload|{'aliases':[aliases[0]|{'expression':'后续圆圈'}]},'synthetic-lex-v2',lex['release_id'])
            resolver.indices.encoder=faulty
            failure_spec={'request_id':'activate-next','expected_generation':3,'expected_serving_epoch':1,'taxonomy_release_id':tax['release_id'],'lexicon_release_id':lex2['release_id'],'note':'Synthetic vocabulary/vector tuple activation'}
            call(client,'POST','/terminology/text-indices/activate',failure_spec,actor='publisher',status=503)
            previous=call(client,'POST','/terminology/resolve',{'query':'合成圆圈'})
            assert previous['state']=='resolved' and previous['pending_publication'] and previous['versions']['lexicon_release_id']==lex['release_id']
            resolver.indices.encoder=original_encoder;faulty.close()
            second=call(client,'POST','/terminology/text-indices/activate',failure_spec,actor='publisher')
            assert second['serving_epoch']==2
            assert call(client,'POST','/terminology/resolve',{'query':'后续圆圈'})['state']=='resolved'
        app,router,_=load_application(config_path)
        with server(app) as url,httpx.Client(base_url=url,timeout=90) as client:
            assert call(client,'GET','/terminology/resolve/status')['serving']['epoch']==2
            call(client,'POST','/terminology/rollback',{'request_id':'rollback','expected_generation':3,'taxonomy_release_id':tax['release_id'],
                 'lexicon_release_id':lex['release_id'],'note':'Synthetic paired rollback'},actor='publisher')
            restored=activate(client,tax,lex,4,2,'restore')
            assert restored['reused'] and restored['index_id']==first['index_id']
            assert call(client,'POST','/terminology/resolve',{'query':'后续圆圈','use_model':False})['state']=='no_match'
            tasks=[]
            for number,text in enumerate(('合成圆圈','圆圈呈现红色')):
                task=ResolveTask(taxonomy_release_id=tax['release_id'],lexicon_release_id=lex['release_id'],embedding_index_id=first['index_id'],request=ResolveInput(query=text,purpose='tag'))
                tasks.append(StepSpec(key='terms-'+str(number),stage='terminology_resolve',config=task.model_dump(mode='json'),versions=Versions(rules_version=contract.version,
                    preprocess_version=PREPROCESS_VERSION,taxonomy_version=tax['version'],lexicon_version=lex['version'],model_revision=registry.revision),limits=TaskLimits(timeout_seconds=30,max_attempts=1,gpu_slots=0)))
            submission=call(client,'POST','/jobs',{'request_id':'real-text-worker','spec':JobSpec(asset_id=asset.asset_id,steps=tuple(tasks),priority=1).model_dump(mode='json')})
            for number in range(2):
                process=subprocess.run([sys.executable,'-m','media_tag','work-once','--config',str(config_path),'--worker-id',principals['worker'].actor_id],capture_output=True,timeout=60)
                (job/f'worker-{number}.json').write_bytes(process.stdout)
                assert process.returncode==0 and json.loads(process.stdout)['outcome']['accepted']
            done=call(client,'GET','/jobs/'+submission['job_id'])
            assert done['state']=='succeeded' and not done['resources']
        with catalog.database.transaction() as c:
            rows=c.execute('''SELECT result_json FROM router_attempts JOIN router_tasks USING(task_id) WHERE job_id=? ORDER BY step_key''',(submission['job_id'],)).fetchall()
            reports=[]
            for row in rows:
                result=json.loads(row[0]);assert not result['suggestions']
                ref=result['artifacts'][0];reports.append(json.loads((folders['workspace']/ref['ref']).read_bytes()))
            counts={table:c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('assets','effective_values','tag_suggestions','index_confirmations','terminology_text_indices')}
            assert counts['assets']==1 and all(counts[t]==0 for t in ('effective_values','tag_suggestions','index_confirmations'))
        assert reports[0]['model_state']=='not_needed' and reports[1]['model_state']=='used'
        assert all(r['source_sha256']==asset.sha256 and not r['medical_evidence_eligible'] for r in reports)
        assert sha256(source.read_bytes()).hexdigest()==source_hash and catalog.get_asset(asset.asset_id,principal=principals['owner']).revision==asset.revision
        assert load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT).file_hashes==contract.file_hashes
        report.update(state='passed',http_requests=calls,model_revision=registry.revision,upstream_revision=registry.manifest['upstream_revision'],
            device='cpu',dimension=512,cold_index_stats=cold_stats,observed_queries=observed,
            warm_http_p50_seconds=statistics.median(measurements),warm_http_p95_seconds=sorted(measurements)[-1],
            latency_sample_count=len(measurements),latency_scope='eight synthetic diagnostics, not D16 business evaluation',
            actual_cli_worker_reports=reports,counts=counts,source_hash_unchanged=True,source_revision_unchanged=True,template_files_unchanged=True,
            offline_degraded_preserves_literal=True,build_failure_retains_previous_tuple=True,restart_and_verified_rollback=True,
            elapsed_seconds=time.monotonic()-started)
        (job/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'state':'passed','report':str(job/'validation.json'),'http_requests':calls}));return 0
    except Exception as exc:
        report.update(state='failed',error=type(exc).__name__,elapsed_seconds=time.monotonic()-started)
        (job/'failure.json').write_text(json.dumps(report,indent=2),encoding='utf-8');raise


if __name__=='__main__':raise SystemExit(main())
