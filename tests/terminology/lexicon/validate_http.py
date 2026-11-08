"""Run with the project .venv from the repo root. Private synthetic HTTP batch only."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from uuid import uuid4

import httpx
import uvicorn

from media_tag.api.app import create_app
from media_tag.catalog import Catalog,Database,Principal
from media_tag.contracts.templates import load_template_bundle
from media_tag.review.models import ReviewDeployment
from media_tag.review.policy import ReviewPolicy
from media_tag.router.service import Router
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.terminology.contracts import Deployment
from media_tag.terminology.lexicon.service import TerminologyService

FINGERPRINT='40365931589406cfc00d5ea29fd2187b63407eaefa3f7361f50c8b7884dc53af'


@contextmanager
def server(app):
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    port=sock.getsockname()[1]
    runtime=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,log_level='error',access_log=False))
    thread=threading.Thread(target=lambda:runtime.run(sockets=[sock]),daemon=True);thread.start()
    try:
        deadline=time.monotonic()+10
        while not runtime.started:
            if not thread.is_alive() or time.monotonic()>deadline:raise RuntimeError('server did not start')
            time.sleep(.025)
        yield f'http://127.0.0.1:{port}'
    finally:
        runtime.should_exit=True;thread.join(10);sock.close()
        if thread.is_alive():raise RuntimeError('server did not stop')


def main():
    root=Path.cwd();job=root/'jobs'/('d13-validation-20261108-'+uuid4().hex[:8]);job.mkdir()
    (job/'README.md').write_text('Private D13 synthetic HTTP publication validation. No official vocabulary or medical acceptance.\n',encoding='utf-8')
    start=time.monotonic();report={'job_id':job.name,'interpreter':sys.executable,'template_fingerprint':FINGERPRINT,
        'synthetic_only':True,'formal_medical_release':False,'model_executed':False}
    try:
        contract=load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT)
        folders={}
        for name in ('workspace','objects'):
            folders[name]=job/name;folders[name].mkdir();(folders[name]/'README.md').write_text('Private D13 engineering data.\n')
        catalog=Catalog(Database(job/'synthetic.sqlite3'),FilesystemBackend(folders['objects'],writable=True),contract=contract)
        router=Router(catalog,folders['workspace'],{})
        principals={name:Principal(actor_id='d13-validation-'+name) for name in ('editor','manager','publisher','other')}
        tokens={name:secrets.token_urlsafe(32) for name in principals}
        config=Deployment(enabled=True,managers=(principals['manager'].actor_id,),publishers=(principals['publisher'].actor_id,))
        def app_for(service):return create_app(router,{sha256(tokens[n].encode()).hexdigest():p for n,p in principals.items()},terminology=service)
        service=TerminologyService(router,config)
        source={'kind':'synthetic','locator':job.name+' abstract fixture','quote':'Abstract symbol A, no medical assertion','usage':'Local engineering validation only'}
        option={'entry_id':'synthetic-option-a','field':'疾病/症状','value':'合成抽象符号甲-仅工程验证','sources':[source],'rationale':'Synthetic validation only'}
        taxpayload={'schema_version':'d13-taxonomy-v1','template_fingerprint':FINGERPRINT,'options':[option]}
        count=0
        def call(client,method,path,data=None,actor='editor',status=200):
            nonlocal count
            response=client.request(method,path,json=data,headers={'Authorization':'Bearer '+tokens[actor]});count+=1
            assert response.status_code==status,(path,response.status_code,response.text)
            assert 'no-store' in response.headers['cache-control']
            return response.json()
        def candidate(client,kind,payload,base=None,suffix=''):
            return call(client,'POST','/terminology/candidates',dict(request_id='create-'+kind+suffix,kind=kind,payload=payload,base_release_id=base,note='Synthetic validation candidate'))
        def reviewed(client,candidate,ids):
            cid=candidate['candidate_id'];path='/terminology/candidates/'+cid
            first=call(client,'POST',path+'/review',dict(request_id='submit-'+cid,expected_revision=candidate['revision'],action='submit',note='Synthetic explicit submission'))
            return call(client,'POST',path+'/review',dict(request_id='approve-'+cid,expected_revision=first['revision'],action='approve',entry_ids=ids,note='Synthetic review of exact fixture entries; not medical acceptance'),actor='manager')
        def publish(client,approved,generation,version):
            data=dict(request_id='publish-'+approved['candidate_id'],expected_revision=approved['revision'],expected_generation=generation,version=version,note='Synthetic fixture release only')
            path='/terminology/candidates/'+approved['candidate_id']+'/publish'
            result=call(client,'POST',path,data,actor='publisher')
            assert call(client,'POST',path,data,actor='publisher')==result
            return result
        with server(app_for(service)) as url,httpx.Client(base_url=url,timeout=30) as client:
            first=candidate(client,'taxonomy',taxpayload)
            assert call(client,'GET','/terminology/candidates/'+first['candidate_id'],actor='other',status=403)
            call(client,'POST','/terminology/candidates/'+first['candidate_id']+'/publish',dict(request_id='early',expected_revision=1,expected_generation=0,version='synthetic-tax-v1',note='Unreviewed attempt'),actor='publisher',status=422)
            assert call(client,'GET','/terminology/active')['generation']==0
            tax=publish(client,reviewed(client,first,['synthetic-option-a']),0,'synthetic-tax-v1')
            lexpayload={'schema_version':'d13-lexicon-v1','template_fingerprint':FINGERPRINT,'taxonomy_release_id':tax['release_id'],
                'concepts':[{'entry_id':'synthetic-concept-a','name':'合成抽象符号甲','concept_type':'symptom','definition':'Engineering fixture with no medical claim',
                    'scope':'Only this synthetic batch','option_ids':['synthetic-option-a'],'sources':[source],'rationale':'Synthetic linkage'}],
                'aliases':[{'entry_id':'synthetic-alias-a','expression':'合成说法甲','target_id':'synthetic-concept-a','relation':'equivalent','default_expand':True,'sources':[source],'rationale':'Synthetic equivalence only'}], 'relations':[]}
            lex=publish(client,reviewed(client,candidate(client,'lexicon',lexpayload),['synthetic-concept-a','synthetic-alias-a']),1,'synthetic-lex-v1')
            second=candidate(client,'taxonomy',taxpayload|{'options':[option,option|{'entry_id':'synthetic-option-b','value':'合成抽象符号乙-仅工程验证'}]},tax['release_id'],'-v2')
            approved=reviewed(client,second,['synthetic-option-a','synthetic-option-b'])
            original_write=service._write
            def fail(folder,name,raw):
                if name=='manifest.json':raise OSError('synthetic injected storage failure')
                return original_write(folder,name,raw)
            service._write=fail
            call(client,'POST','/terminology/candidates/'+second['candidate_id']+'/publish',dict(request_id='publish-'+second['candidate_id'],expected_revision=approved['revision'],expected_generation=2,version='synthetic-tax-v2',note='Synthetic fixture release only'),actor='publisher',status=503)
            assert call(client,'GET','/terminology/active')['lexicon_release_id']==lex['release_id']
            service._write=original_write
            tax2=publish(client,approved,2,'synthetic-tax-v2')
            assert call(client,'GET','/terminology/active')['lexicon_release_id'] is None
        # New service and real listener against the same persisted DB/files.
        service=TerminologyService(router,config)
        with server(app_for(service)) as url,httpx.Client(base_url=url,timeout=30) as client:
            assert call(client,'GET','/terminology/active')['generation']==3
            rollback=dict(request_id='rollback',expected_generation=3,taxonomy_release_id=tax['release_id'],lexicon_release_id=lex['release_id'],note='Synthetic paired rollback after service restart')
            active=call(client,'POST','/terminology/rollback',rollback,actor='publisher')
            assert active['generation']==4 and active['lexicon_release_id']==lex['release_id']
            assert call(client,'POST','/terminology/rollback',rollback,actor='publisher')==active
            call(client,'POST','/terminology/rollback',rollback|{'request_id':'stale'},actor='publisher',status=409)
        manifest_path=folders['workspace']/tax['manifest_ref']
        policy=ReviewPolicy(contract,ReviewDeployment(enabled=True,taxonomy_manifest=str(manifest_path),taxonomy_version=tax['version'],
                            taxonomy_manifest_sha256=tax['manifest_sha256'],trusted_publishers=(principals['publisher'].actor_id,)))
        assert policy.options(policy.asset_fields['疾病/症状'])==(option['value'],)
        with catalog.database.transaction() as c:
            counts={table:c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('terminology_releases','terminology_events','assets','router_jobs','effective_values','index_confirmations')}
        assert counts['terminology_releases']==3 and all(counts[t]==0 for t in ('assets','router_jobs','effective_values','index_confirmations'))
        after=load_template_bundle(root/'asset-index-template',expected_fingerprint=FINGERPRINT)
        assert after.file_hashes==contract.file_hashes
        report.update(state='passed',http_requests=count,active=active,counts=counts,d06_consumer='passed',
            template_files_unchanged=True,release_manifests=[str((folders['workspace']/r['manifest_ref']).relative_to(job)) for r in (tax,lex,tax2)],
            failure_injection='manifest write ->503, old pair retained, same request retry succeeded',
            restart_and_pair_rollback='passed',elapsed_seconds=time.monotonic()-start)
        (job/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'state':'passed','report':str(job/'validation.json'),'http_requests':count}));return 0
    except Exception as exc:
        report.update(state='failed',error=type(exc).__name__,elapsed_seconds=time.monotonic()-start)
        (job/'failure.json').write_text(json.dumps(report,indent=2),encoding='utf-8');raise


if __name__=='__main__':raise SystemExit(main())
