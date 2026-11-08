"""Bounded synchronous release builds, with one transactional activation pointer.

This is the trusted server side, not a Worker: it never creates visual jobs or edits
asset metadata. D14 will build paired text indices before a future activation.
"""
from hashlib import sha256
from datetime import datetime
import json
import os
from time import monotonic
from uuid import uuid4

from pydantic import ValidationError
from media_tag.catalog import AccessDenied, NotFound, RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import _human, encode
from media_tag.contracts.vocabulary import VocabularyManifest
from media_tag.storage import IntegrityError
from media_tag.terminology.contracts import Taxonomy, Lexicon, normalize
from media_tag.workers.evidence import _directory
from .validation import entries, issues, option_fields


class PublicationBlocked(ValueError):
    def __init__(self, problems):
        self.problems = problems
        super().__init__('vocabulary publication validation failed')


def parse_payload(kind, data):
    return (Taxonomy if kind == 'taxonomy' else Lexicon).model_validate_json(encode(data))


def fingerprint(value): return sha256(encode(value).encode()).hexdigest()


class TerminologyService:
    def __init__(self, router, config):
        self.router, self.database, self.contract, self.config = router, router.database, router.catalog.contract, config
        if not config.enabled: raise ValueError('terminology service is disabled')

    def _manager(self, principal, *, publish=False):
        _human(principal)
        allowed = self.config.publishers if publish else self.config.managers
        if principal.actor_id not in allowed: raise AccessDenied('explicit terminology capability required')

    def _candidate(self, c, candidate_id, principal):
        _human(principal)
        row = c.execute('SELECT * FROM terminology_candidates WHERE candidate_id=?',(candidate_id,)).fetchone()
        if row is None: raise NotFound('candidate unavailable')
        if row['owner_id'] != principal.actor_id and principal.actor_id not in (*self.config.managers,*self.config.publishers):
            raise AccessDenied('candidate permission denied')
        return row

    @staticmethod
    def _expected(row, revision):
        if row['revision'] != revision: raise RevisionConflict('candidate changed; reload')

    def _replay(self, c, principal, request_id, data):
        digest = fingerprint(data)
        row = c.execute('SELECT * FROM terminology_requests WHERE actor_id=? AND request_id=?',(principal.actor_id,request_id)).fetchone()
        if row:
            if row['fingerprint'] != digest: raise RevisionConflict('request id reused for different input')
            return json.loads(row['result_json'])
        return None

    def _done(self, c, principal, request_id, data, result, action, candidate_id=None):
        c.execute('INSERT INTO terminology_requests VALUES(?,?,?,?)',(principal.actor_id,request_id,fingerprint(data),encode(result)))
        c.execute('INSERT INTO terminology_events(actor_id,candidate_id,action,payload_json,created_at) VALUES(?,?,?,?,?)',
                  (principal.actor_id,candidate_id,action,encode({'input':data,'result':result}),utc_now()))
        return result

    @staticmethod
    def _head(c): return dict(c.execute('SELECT generation,taxonomy_release_id,lexicon_release_id FROM terminology_head').fetchone())

    def _release(self, c, release_id):
        row = c.execute('SELECT * FROM terminology_releases WHERE release_id=?',(release_id,)).fetchone()
        if row is None: raise NotFound('release unavailable')
        bundle = json.loads(row['bundle_json'])
        for name, digest in bundle['files'].items():
            with self.router.files.open_read(f"{bundle['directory']}/{name}") as stream: raw = stream.read(32*1024**2+1)
            if len(raw)>32*1024**2 or sha256(raw).hexdigest()!=digest: raise IntegrityError('release bundle changed')
        payload = parse_payload(row['kind'],json.loads(row['payload_json']))
        return row, payload

    def _taxonomy(self, c, payload):
        if isinstance(payload, Taxonomy): return None
        row, taxonomy = self._release(c,payload.taxonomy_release_id)
        if row['kind'] != 'taxonomy': raise ValueError('lexicon must reference a taxonomy release')
        return taxonomy

    def _issues(self, c, payload):
        kind = 'taxonomy' if isinstance(payload,Taxonomy) else 'lexicon'
        previous = [parse_payload(kind,json.loads(r[0])) for r in c.execute('SELECT payload_json FROM terminology_releases WHERE kind=?',(kind,))]
        try: taxonomy = self._taxonomy(c,payload)
        except NotFound: return [{'code':'missing_published_taxonomy'}]
        except ValueError: return [{'code':'invalid_taxonomy_reference'}]
        return issues(payload,self.contract,taxonomy,previous)

    def submit(self, spec, *, principal):
        _human(principal)
        data = {'action':'create',**spec.model_dump(mode='json')}
        with self.database.transaction(write=True) as c:
            replay = self._replay(c,principal,spec.request_id,data)
            if replay: return replay
            if spec.payload.template_fingerprint != self.contract.fingerprint: raise ValueError('template mismatch')
            head = self._head(c)
            if spec.base_release_id != head[f'{spec.kind}_release_id']: raise RevisionConflict('base release changed')
            if spec.base_release_id:
                row,_ = self._release(c,spec.base_release_id)
                if row['kind'] != spec.kind: raise ValueError('base kind mismatch')
            candidate_id = 'candidate-'+uuid4().hex
            c.execute('INSERT INTO terminology_candidates VALUES(?,?,?,?,?,?,?,?,?)',
                (candidate_id,spec.kind,principal.actor_id,spec.base_release_id,1,'draft',encode(spec.payload.model_dump(mode='json')),'{}',utc_now()))
            result = {'candidate_id':candidate_id,'revision':1,'state':'draft'}
            return self._done(c,principal,spec.request_id,data,result,'create',candidate_id)

    def get(self, candidate_id, *, principal):
        with self.database.transaction() as c:
            row = self._candidate(c,candidate_id,principal)
            payload = parse_payload(row['kind'],json.loads(row['payload_json']))
            return {k:row[k] for k in ('candidate_id','kind','owner_id','base_release_id','revision','state')} | {
                'payload':payload.model_dump(mode='json'),'approvals':json.loads(row['approvals_json']),
                'issues':self._issues(c,payload)}

    def list_candidates(self, *, principal):
        _human(principal)
        manager = principal.actor_id in (*self.config.managers,*self.config.publishers)
        with self.database.transaction() as c:
            return {'items':[dict(r) for r in c.execute('''SELECT candidate_id,kind,revision,state,owner_id FROM terminology_candidates
                WHERE ? OR owner_id=? ORDER BY created_at DESC LIMIT 200''',(int(manager),principal.actor_id))],'limit':200}

    def revise(self, candidate_id, spec, *, principal):
        data = {'action':'revise','candidate_id':candidate_id,**spec.model_dump(mode='json')}
        with self.database.transaction(write=True) as c:
            row = self._candidate(c,candidate_id,principal)
            replay = self._replay(c,principal,spec.request_id,data)
            if replay: return replay
            self._expected(row,spec.expected_revision)
            if row['state']=='published': raise RevisionConflict('create a new candidate for a published version')
            if (row['kind']=='taxonomy') != isinstance(spec.payload,Taxonomy) or spec.payload.template_fingerprint != self.contract.fingerprint:
                raise ValueError('payload contract mismatch')
            c.execute("UPDATE terminology_candidates SET revision=revision+1,state='draft',payload_json=?,approvals_json='{}' WHERE candidate_id=?",
                      (encode(spec.payload.model_dump(mode='json')),candidate_id))
            return self._done(c,principal,spec.request_id,data,{'candidate_id':candidate_id,'revision':row['revision']+1,'state':'draft'},'revise',candidate_id)

    def review(self, candidate_id, spec, *, principal):
        if spec.action!='submit': self._manager(principal)
        data = {'action':'review','candidate_id':candidate_id,**spec.model_dump(mode='json')}
        with self.database.transaction(write=True) as c:
            row = self._candidate(c,candidate_id,principal)
            replay = self._replay(c,principal,spec.request_id,data)
            if replay: return replay
            self._expected(row,spec.expected_revision)
            state = row['state']; approvals = json.loads(row['approvals_json'])
            if spec.action=='submit':
                if state!='draft' or spec.entry_ids: raise RevisionConflict('only a draft can be submitted')
                state='review'
            elif spec.action=='reject':
                if state!='review' or spec.entry_ids: raise RevisionConflict('only a review can be rejected')
                state='rejected'; approvals={}
            else:
                if state!='review': raise RevisionConflict('candidate is not under review')
                payload = parse_payload(row['kind'],json.loads(row['payload_json']))
                problems = self._issues(c,payload)
                if problems: raise PublicationBlocked(problems)
                objects = {e.entry_id:e for _,e in entries(payload)}
                if not spec.entry_ids or len(set(spec.entry_ids)) != len(spec.entry_ids) or not set(spec.entry_ids)<=objects.keys():
                    raise ValueError('explicit unique entry ids required')
                for eid in spec.entry_ids:
                    approvals[eid]={'entry_sha256':fingerprint(objects[eid].model_dump(mode='json')),'actor_id':principal.actor_id,'at':utc_now(),'note':spec.note}
                if set(approvals)==objects.keys(): state='approved'
            c.execute('UPDATE terminology_candidates SET revision=revision+1,state=?,approvals_json=? WHERE candidate_id=?',(state,encode(approvals),candidate_id))
            return self._done(c,principal,spec.request_id,data,{'candidate_id':candidate_id,'revision':row['revision']+1,'state':state},spec.action,candidate_id)

    def import_rows(self, candidate_id, spec, *, principal):
        from media_tag.terminology.contracts import TaxonomyOption,Concept,Alias,Relation
        models={'options':TaxonomyOption,'concepts':Concept,'aliases':Alias,'relations':Relation}
        data={'action':'import','candidate_id':candidate_id,**spec.model_dump(mode='json')}
        with self.database.transaction(write=True) as c:
            row=self._candidate(c,candidate_id,principal)
            replay=self._replay(c,principal,spec.request_id,data)
            if replay:return replay
            self._expected(row,spec.expected_revision)
            if row['state']=='published':raise RevisionConflict('published candidate is immutable')
            payload=parse_payload(row['kind'],json.loads(row['payload_json']))
            if not hasattr(payload,spec.entity):raise ValueError('wrong import entity')
            values=list(getattr(payload,spec.entity)); reports=[]
            for index,raw in enumerate(spec.rows,1):
                try: entry=models[spec.entity].model_validate_json(encode(raw))
                except ValidationError:
                    reports.append({'row':index,'status':'invalid','codes':['invalid_contract']});continue
                status='added'; duplicate=None
                identity=next((old for old in values if old.entry_id==entry.entry_id),None)
                if identity is not None:
                    left=identity.model_dump(mode='json',exclude={'sources'})
                    right=entry.model_dump(mode='json',exclude={'sources'})
                    if left!=right:
                        reports.append({'row':index,'status':'conflict','codes':['entry_id_conflict']});continue
                for i,old in enumerate(values):
                    left=old.model_dump(mode='json',exclude={'entry_id','sources'});right=entry.model_dump(mode='json',exclude={'entry_id','sources'})
                    if spec.entity=='aliases':
                        left['expression']=normalize(left['expression']);right['expression']=normalize(right['expression'])
                        for key in ('rationale','default_expand'):left.pop(key,None);right.pop(key,None)
                    if left==right:duplicate=i;break
                    if old.entry_id==entry.entry_id:status='conflict';break
                if status=='conflict':reports.append({'row':index,'status':status,'codes':['entry_id_conflict']});continue
                if duplicate is not None:
                    old=values[duplicate]; sources=list(old.sources)
                    for source in entry.sources:
                        if source not in sources:sources.append(source)
                    merged=old.model_dump(mode='json');merged['sources']=[s.model_dump(mode='json') for s in sources]
                    if spec.entity=='aliases':
                        if old.rationale!=entry.rationale:merged['rationale']=old.rationale+'\n'+entry.rationale
                        merged['default_expand']=old.default_expand and entry.default_expand
                    try:values[duplicate]=models[spec.entity].model_validate_json(encode(merged))
                    except ValidationError:
                        reports.append({'row':index,'status':'invalid','codes':['merged_entry_budget_exceeded']});continue
                    status='merged'
                else:values.append(entry)
                reports.append({'row':index,'status':status,'entry_id':values[duplicate].entry_id if duplicate is not None else entry.entry_id})
            changed=payload.model_dump(mode='json');changed[spec.entity]=[e.model_dump(mode='json') for e in values]
            changed=parse_payload(row['kind'],changed)
            problems=self._issues(c,changed)
            for report in reports:
                if 'entry_id' in report:
                    report['publication_issues']=[i for i in problems if i.get('entry_id')==report['entry_id']]
            # Preserve valid unknown-target/missing-source candidates; publication remains blocked.
            can_apply=all(r['status'] not in ('invalid','conflict') for r in reports)
            applied=bool(spec.apply and can_apply)
            revision=row['revision']+int(applied)
            if applied:
                c.execute("UPDATE terminology_candidates SET revision=?,state='draft',payload_json=?,approvals_json='{}' WHERE candidate_id=?",
                          (revision,encode(changed.model_dump(mode='json')),candidate_id))
            result={'candidate_id':candidate_id,'revision':revision,'applied':applied,'rows':reports,'publication_issues':problems,
                    'atomic_import':True,'scope':spec.entity}
            return self._done(c,principal,spec.request_id,data,result,'import',candidate_id)

    def _diff(self, c, row, payload, principal):
        previous=None
        if row['base_release_id']: _,previous=self._release(c,row['base_release_id'])
        before={e.entry_id:e.model_dump(mode='json') for _,e in entries(previous)} if previous else {}
        after={e.entry_id:e.model_dump(mode='json') for _,e in entries(payload)}
        added=sorted(after.keys()-before.keys());removed=sorted(before.keys()-after.keys())
        changed=sorted(k for k in after.keys() & before.keys() if before[k]!=after[k])
        semantic=[]
        if isinstance(payload,Lexicon):
            for concept in payload.concepts:
                old=before.get(concept.entry_id)
                current=concept.model_dump(mode='json')
                if old and any(old[k]!=current[k] for k in ('definition','scope','option_ids','status','replacements')):
                    semantic.append(concept.entry_id)
        impacted_fields={}
        if isinstance(payload,Taxonomy):
            for key in changed+removed:
                old=before[key];impacted_fields.setdefault(old['field'],set()).add(old['value'])
        elif semantic:
            taxonomy=self._taxonomy(c,payload);options={o.entry_id:o for o in taxonomy.options}
            for cid in semantic:
                for oid in set(before[cid]['option_ids']) | set(after[cid]['option_ids']):
                    if oid in options:
                        option=options[oid];impacted_fields.setdefault(option.field,set()).add(option.value)
        affected=set()
        if impacted_fields:
            for r in c.execute('SELECT asset_id,field,payload FROM effective_values'):
                if r['field'] not in impacted_fields:continue
                value=json.loads(r['payload'])['value']
                if not set(value.get('values') or ()) & impacted_fields[r['field']]:continue
                try:self.router.catalog._authorize(c,r['asset_id'],principal)
                except (AccessDenied,NotFound):continue
                affected.add(r['asset_id'])
        if semantic:
            placeholders=','.join('?' for _ in semantic)
            for row in c.execute(f'SELECT DISTINCT asset_id FROM asset_concept_links WHERE active=1 AND concept_id IN ({placeholders})',semantic):
                try:self.router.catalog._authorize(c,row['asset_id'],principal)
                except (AccessDenied,NotFound):continue
                affected.add(row['asset_id'])
        return {'added':added,'changed':changed,'removed':removed,'semantic_concepts':semantic,'affected_asset_ids':sorted(affected),
                'impact_scope':'currently authorized effective taxonomy fields and active human concept links',
                'visual_reanalysis':False,'human_values_rewritten':False,'invalidate':['query_cache','text_index'],
                'lexicon_deactivated':row['kind']=='taxonomy' and self._head(c)['lexicon_release_id'] is not None}

    def preview(self,candidate_id,*,principal):
        self._manager(principal)
        with self.database.transaction() as c:
            row=self._candidate(c,candidate_id,principal);payload=parse_payload(row['kind'],json.loads(row['payload_json']))
            return {'generation':self._head(c)['generation'],'revision':row['revision'],'issues':self._issues(c,payload),
                    'diff':self._diff(c,row,payload,principal)}

    def _write(self, folder, name, raw):
        if len(raw)>32*1024**2:raise ValueError('release file budget exceeded')
        with (folder/name).open('xb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())

    def publish(self,candidate_id,spec,*,principal):
        self._manager(principal,publish=True)
        data={'action':'publish','candidate_id':candidate_id,**spec.model_dump(mode='json')}
        batch=None;started=monotonic()
        try:
            # Synchronous bounded build under BEGIN IMMEDIATE: serializes candidate edits,
            # competing publishers and head CAS. No unleased asynchronous worker implied.
            with self.database.transaction(write=True) as c:
                row=self._candidate(c,candidate_id,principal)
                replay=self._replay(c,principal,spec.request_id,data)
                if replay:
                    self._release(c,replay['release_id']);return replay
                self._expected(row,spec.expected_revision);head=self._head(c)
                if head['generation']!=spec.expected_generation or head[f"{row['kind']}_release_id"]!=row['base_release_id']:
                    raise RevisionConflict('publication base changed')
                if row['state']!='approved':raise PublicationBlocked([{'code':'unreviewed_entries'}])
                payload=parse_payload(row['kind'],json.loads(row['payload_json']))
                problems=self._issues(c,payload)
                approvals=json.loads(row['approvals_json'])
                for _,entry in entries(payload):
                    approval=approvals.get(entry.entry_id)
                    if not approval or approval['entry_sha256']!=fingerprint(entry.model_dump(mode='json')) or approval['actor_id'] not in self.config.managers:
                        problems.append({'code':'missing_current_approval','entry_id':entry.entry_id})
                if problems:raise PublicationBlocked(problems)
                if isinstance(payload,Lexicon) and payload.taxonomy_release_id!=head['taxonomy_release_id']:
                    raise RevisionConflict('active taxonomy changed')
                if c.execute('SELECT 1 FROM terminology_releases WHERE kind=? AND version=?',(row['kind'],spec.version)).fetchone():
                    raise RevisionConflict('version is immutable')
                diff=self._diff(c,row,payload,principal);release_id='release-'+uuid4().hex
                relative='jobs/terminology-'+uuid4().hex;batch=_directory(self.router.workspace,relative)
                (batch/'README.md').write_text('Private D13 release attempt; DB head alone determines activation. No medical acceptance.\n',encoding='utf-8')
                timestamp=utc_now();content=payload.model_dump(mode='json')
                if isinstance(payload,Taxonomy):content['fields']=option_fields(payload,self.contract)
                raw=encode(content).encode()
                taxrow=None
                if isinstance(payload,Lexicon):taxrow,_=self._release(c,payload.taxonomy_release_id)
                manifest=VocabularyManifest(kind=row['kind'],version=spec.version,status='published',content_file='content.json',
                    content_sha256=sha256(raw).hexdigest(),taxonomy_version=taxrow['version'] if taxrow else None,
                    published_by=principal.actor_id,published_at=datetime.fromisoformat(timestamp))
                values={'content.json':raw,'manifest.json':encode(manifest.model_dump(mode='json')).encode(),
                        'diff.json':encode(diff).encode(),'review.json':encode(approvals).encode(),
                        'inputs.json':encode({'candidate_id':candidate_id,'revision':row['revision'],'generation':head['generation'],
                                             'payload_sha256':fingerprint(payload.model_dump(mode='json')),'request':data}).encode()}
                bundle={'directory':relative,'files':{name:sha256(raw).hexdigest() for name,raw in values.items()}}
                for name,raw in values.items():self._write(batch,name,raw)
                self._write(batch,'result.json',encode({'state':'validated','elapsed_seconds':monotonic()-started,'files':bundle['files']}).encode())
                c.execute('INSERT INTO terminology_releases VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (release_id,row['kind'],spec.version,candidate_id,row['revision'],payload.taxonomy_release_id if taxrow else None,
                           encode(payload.model_dump(mode='json')),encode(bundle),principal.actor_id,timestamp))
                self._release(c,release_id)  # Reopen and rehash before the pointer update.
                taxonomy_id=release_id if row['kind']=='taxonomy' else head['taxonomy_release_id']
                lexicon_id=release_id if row['kind']=='lexicon' else None
                c.execute('UPDATE terminology_head SET generation=generation+1,taxonomy_release_id=?,lexicon_release_id=?',(taxonomy_id,lexicon_id))
                c.execute("UPDATE terminology_candidates SET state='published',revision=revision+1 WHERE candidate_id=?",(candidate_id,))
                result={'release_id':release_id,'version':spec.version,'kind':row['kind'],'generation':head['generation']+1,
                        'manifest_ref':relative+'/manifest.json','manifest_sha256':bundle['files']['manifest.json'],
                        'taxonomy_release_id':taxonomy_id,'lexicon_release_id':lexicon_id,'diff':diff}
                return self._done(c,principal,spec.request_id,data,result,'publish',candidate_id)
        except Exception as exc:
            if batch is not None:
                # Diagnostic only, never an active pointer. Do not echo operator text or paths.
                try:self._write(batch,'failure.json',encode({'state':'failed','error':type(exc).__name__,'elapsed_seconds':monotonic()-started}).encode())
                except OSError:pass
            raise

    def active(self, *, principal):
        _human(principal)
        with self.database.transaction() as c:
            head=self._head(c)
            for key in ('taxonomy_release_id','lexicon_release_id'):
                if head[key]:self._release(c,head[key])
            return head | {'model_index':None,'resolver_available':False}

    def release(self,release_id,*,principal):
        self._manager(principal)
        with self.database.transaction() as c:
            row,payload=self._release(c,release_id)
            return {'release_id':release_id,'kind':row['kind'],'version':row['version'],'payload':payload.model_dump(mode='json'),
                    'published_by':row['published_by'],'published_at':row['published_at'],'bundle':json.loads(row['bundle_json'])}

    def rollback(self,spec,*,principal):
        self._manager(principal,publish=True)
        data={'action':'rollback',**spec.model_dump(mode='json')}
        with self.database.transaction(write=True) as c:
            replay=self._replay(c,principal,spec.request_id,data)
            if replay:
                self._release(c,spec.taxonomy_release_id)
                if spec.lexicon_release_id:self._release(c,spec.lexicon_release_id)
                return replay
            head=self._head(c)
            if head['generation']!=spec.expected_generation:raise RevisionConflict('active version changed')
            taxrow,_=self._release(c,spec.taxonomy_release_id)
            if taxrow['kind']!='taxonomy':raise ValueError('taxonomy required')
            if spec.lexicon_release_id:
                lexrow,_=self._release(c,spec.lexicon_release_id)
                if lexrow['kind']!='lexicon' or lexrow['taxonomy_release_id']!=spec.taxonomy_release_id:raise ValueError('incompatible rollback pair')
            c.execute('UPDATE terminology_head SET generation=generation+1,taxonomy_release_id=?,lexicon_release_id=?',
                      (spec.taxonomy_release_id,spec.lexicon_release_id))
            result=self._head(c)|{'invalidate':['query_cache','text_index'],'visual_reanalysis':False,'human_values_rewritten':False}
            return self._done(c,principal,spec.request_id,data,result,'rollback')
