"""Query expansion never grants asset access or supplies missing medical evidence."""
import base64
from hashlib import sha256
import hmac
import json
import secrets
import subprocess
from types import SimpleNamespace
from media_tag.catalog import AccessDenied,NotFound,RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import encode
from media_tag.contracts.models import EffectiveValue,TaskEnvelope,WorkerResult
from media_tag.files.service import FileService,FileError
from media_tag.review.policy import MEDICAL,index_status
from media_tag.storage import StorageError
from media_tag.terminology.contracts import normalize
from media_tag.terminology.lexicon.service import fingerprint
from media_tag.terminology.resolve.matching import exact_matches,intent
from media_tag.web.service import LibraryService
from media_tag.terminology.resolve.models import QueryInputError


def concept_fingerprint(concept):
    return fingerprint(concept.model_dump(mode='json',include={'entry_id','concept_type','definition','scope','option_ids','status'}))


class SearchService:
    def __init__(self,resolver,*,reviews=None,options=None,visual_encoder=None):
        from .models import SearchOptions
        self.resolver,self.terminology=resolver,resolver.terminology
        self.router,self.catalog,self.database=resolver.terminology.router,resolver.terminology.router.catalog,resolver.terminology.database
        self.files=FileService(self.router);self.library=LibraryService(self.router)
        self.reviews,self.options=reviews,options or SearchOptions(enabled=True)
        if not self.options.enabled:raise ValueError('search is disabled')
        self.cursor_secret=secrets.token_bytes(32)
        from media_tag.drafts.service import DraftIndex
        self.drafts=DraftIndex(self.router);self.visual_encoder=visual_encoder
        from .links import ConceptLinks
        from .feedback import FeedbackService
        from .imports import TermImports
        from .aliases import AliasExport
        self.links=ConceptLinks(self)
        self.feedback=FeedbackService(self)
        self.imports=TermImports(self)
        self.aliases=AliasExport(self)
        if reviews:reviews.search=self

    @staticmethod
    def _reader(principal):
        if principal.role not in ('reader','editor','admin'):raise AccessDenied('personnel read capability required')

    def vocabulary(self,*,principal):
        self._reader(principal)
        try:taxonomy,lexicon,_,_,versions=self.resolver._snapshot()
        except (StorageError,ValueError,NotFound):return {'concepts':[],'options':[],'versions':None,'state':'unavailable'}
        medical_ids={o.entry_id for o in taxonomy.options if o.field in MEDICAL}
        return {'concepts':[{'concept_id':c.entry_id,'name':c.name,'concept_type':c.concept_type,
                            'requires_medical_review':c.concept_type!='other' or bool(set(c.option_ids)&medical_ids)} for c in lexicon.concepts if c.status=='active'],
            'options':[{'option_id':o.entry_id,'field':o.field,'value':o.value} for o in taxonomy.options if o.status=='active'],
            'versions':versions,'state':'published'}

    def _document(self,c,asset,taxonomy_id,*,persist=False):
        records=list(c.execute('SELECT field,payload FROM effective_values WHERE asset_id=? ORDER BY field',(asset['asset_id'],)))
        aliases=[r[0] for r in c.execute('SELECT alias FROM asset_aliases WHERE asset_id=? ORDER BY namespace,alias',(asset['asset_id'],))]
        basis=fingerprint({'source':asset['blob_sha256'],'revision':asset['revision'],'values':[tuple(r) for r in records],'aliases':aliases,
            'taxonomy':taxonomy_id,'policy':getattr(getattr(self.reviews,'policy',None),'digest',None)})
        old=c.execute('SELECT payload_json FROM search_documents WHERE asset_id=? AND basis_sha256=?',(asset['asset_id'],basis)).fetchone()
        if old:return json.loads(old[0])
        values=[];medical=[]
        for row in records:
            effective=EffectiveValue.model_validate_json(row['payload'])
            if effective.value.state!='observed':continue
            valid=True
            if effective.field in MEDICAL:
                review=c.execute("SELECT payload_json FROM review_decisions WHERE asset_id=? AND field=? AND asset_revision=? AND decision!='reject'",(asset['asset_id'],effective.field,effective.revision)).fetchone()
                valid=bool(review and json.loads(review[0]).get('effective')==effective.model_dump(mode='json'))
            if not valid:continue
            values.append({'field':effective.field,'values':list(effective.value.values),'source':'human_effective','revision':effective.revision})
            if effective.field in MEDICAL:medical.append(values[-1])
        result={'name':asset['name'],'aliases':aliases,'fields':values,'medical_fields':medical}
        if persist:c.execute('''INSERT INTO search_documents VALUES(?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET
            source_sha256=excluded.source_sha256,revision=excluded.revision,basis_sha256=excluded.basis_sha256,
            payload_json=excluded.payload_json,updated_at=excluded.updated_at''',
            (asset['asset_id'],asset['blob_sha256'],asset['revision'],basis,encode(result),utc_now()))
        return result

    def _drafts(self,c,asset,lexicon):
        concepts={con.entry_id:con for con in lexicon.concepts if con.status=='active'} if lexicon else {}
        found=[]
        rows=c.execute('''SELECT a.envelope_json,a.result_json,t.spec_json FROM router_attempts a JOIN router_tasks t USING(task_id)
            JOIN router_jobs j USING(job_id) WHERE j.asset_id=? AND j.cancelled=0 AND t.active=1 AND t.state='succeeded'
            AND a.lease_epoch=t.lease_epoch AND a.released_at IS NOT NULL ORDER BY a.started_at DESC LIMIT 16''',(asset['asset_id'],))
        for row in rows:
            try:
                task=TaskEnvelope.model_validate_json(row['envelope_json']);result=WorkerResult.model_validate_json(row['result_json'])
                if task.asset_revision!=asset['revision'] or task.input_refs[0].sha256!=asset['blob_sha256']:continue
                if task.stage!='terminology_resolve':continue
                self.router._artifacts(task,result)
                for ref in result.artifacts:
                    if ref.kind not in ('model_report','terminology_report'):continue
                    with self.router.files.open_read(ref.ref) as stream:raw=stream.read(8*1024**2+1)
                    if len(raw)>8*1024**2 or sha256(raw).hexdigest()!=ref.sha256:continue
                    report=json.loads(raw)
                    if report.get('source_sha256')!=asset['blob_sha256']:continue
                    if ref.kind=='terminology_report' and lexicon and report.get('versions',{}).get('lexicon_release_id'):
                        _,sourcelex=self.terminology._release(c,report['versions']['lexicon_release_id'])
                        if not hasattr(sourcelex,'concepts') or report.get('input_fingerprint')!=task.input_fingerprint or report.get('asset_revision')!=asset['revision']:continue
                        previous={con.entry_id:con for con in sourcelex.concepts if con.status=='active'}
                        for candidate in report.get('candidates',[]):
                            cid=candidate.get('concept_id')
                            if cid in concepts and cid in previous and concept_fingerprint(concepts[cid])==concept_fingerprint(previous[cid]) and candidate.get('context_state')!='excluded':
                                found.append({'kind':'concept_suggestion','concept_id':candidate['concept_id'],'intent':candidate.get('intent',{}),
                                    'text':concepts[candidate['concept_id']].name,'task_id':task.task_id,'evidence_origin':report.get('evidence_origin')})
            except (StorageError,ValueError,NotFound,KeyError,TypeError):continue
        return found

    def _visible(self,c,principal,spec):
        rows=c.execute('''SELECT a.* FROM assets a LEFT JOIN library_entries e USING(asset_id)
            WHERE a.deleted_at IS NULL AND (a.owner_id=? OR EXISTS(SELECT 1 FROM asset_permissions p
                WHERE p.asset_id=a.asset_id AND p.subject_id=? AND p.permission IN ('read','edit')))
            AND (?=0 OR a.owner_id=?) AND ((?=1 AND a.owner_id=?) OR coalesce(e.active,
                EXISTS(SELECT 1 FROM uploads u WHERE u.asset_id=a.asset_id AND u.state='completed'
                    AND json_extract(u.spec_json,'$.purpose')='library'))=1)
            ORDER BY a.root_asset_id,a.asset_id LIMIT ?''',
            (principal.actor_id,principal.actor_id,int(spec.mine),principal.actor_id,int(spec.workspace),principal.actor_id,self.options.max_assets+1)).fetchall()
        if len(rows)>self.options.max_assets:raise FileError(503,'search_asset_budget_exceeded',retryable=False)
        return [r for r in rows if not self.files._gate(c,r)]

    def _cursor(self,scope,last):
        raw=encode({'scope':scope,'last':last}).encode()
        return base64.urlsafe_b64encode(raw+hmac.digest(self.cursor_secret,raw,'sha256')).decode().rstrip('=')

    def _after(self,cursor,scope):
        if not cursor:return None
        try:
            value=base64.urlsafe_b64decode(cursor+'='*(-len(cursor)%4));raw,signature=value[:-32],value[-32:]
            if not hmac.compare_digest(signature,hmac.digest(self.cursor_secret,raw,'sha256')):raise ValueError()
            data=json.loads(raw)
            if data['scope']!=scope:raise RevisionConflict('search results changed; refresh the first page')
            last=data['last']
            if not isinstance(last,list) or len(last)!=2 or type(last[0]) is not int or not isinstance(last[1],str):raise ValueError()
            return tuple(last)
        except (ValueError,KeyError,TypeError):raise QueryInputError('invalid search cursor') from None

    def search(self,spec,*,principal,trial=None):
        self._reader(principal)
        from .filters import validate_filters,passes
        validate_filters(self,spec.filters)
        if trial is None:
            resolution=self.resolver.resolve(spec.request.model_copy(update={'use_model':False}) if spec.literal_only else spec.request,principal=principal)
            versions=resolution.get('versions')
            with self.database.transaction() as c:
                taxonomy,lexicon,_=self.resolver.indices.pair(c,versions['taxonomy_release_id'],versions['lexicon_release_id']) if versions else (None,None,None)
        else:
            lexicon,revision=trial
            with self.database.transaction() as c:_,taxonomy=self.terminology._release(c,lexicon.taxonomy_release_id)
            matches,errors=exact_matches(lexicon,spec.request)
            valid=[m for m in matches if m['context_state']!='excluded']
            ready=[m for m in valid if m['default_expand']]
            state='resolved' if len({m['concept_id'] for m in valid})==1 and ready and not errors else 'ambiguous' if valid else 'no_match'
            versions={'taxonomy_release_id':lexicon.taxonomy_release_id,'lexicon_release_id':'draft','draft_revision':revision,'content_sha256':fingerprint(lexicon.model_dump(mode='json'))}
            resolution={'state':state,'query':spec.request.query,'literal_query':spec.request.query,'candidates':valid,'matches':matches,
                        'errors':errors,'model_state':'not_used_for_draft','versions':versions,'draft_trial':True}
        concepts={con.entry_id:con for con in lexicon.concepts if con.status=='active'} if lexicon else {}
        if len(set(spec.selected_concept_ids))!=len(spec.selected_concept_ids) or not set(spec.selected_concept_ids)<=concepts.keys():raise QueryInputError('select active published concepts')
        selected=set(spec.selected_concept_ids)
        if selected and (spec.literal_only or not versions or spec.selection_lexicon_release_id!=versions['lexicon_release_id']):raise RevisionConflict('selected concept version changed; choose again')
        if not selected and not spec.literal_only and resolution['state']=='resolved':selected={m['concept_id'] for m in resolution['candidates'] if m['default_expand']}
        query=normalize(spec.request.query)
        meaning=intent(spec.request.query,0,len(spec.request.query),spec.request.context)
        matched_intents=[m['intent'] for m in resolution.get('matches',[]) if m.get('concept_id') in selected]
        if matched_intents:
            polarities={m['polarity'] for m in matched_intents};directions={m['direction'] for m in matched_intents}
            if len(polarities)==1:meaning['polarity']=next(iter(polarities))
            if len(directions)==1:meaning['direction']=next(iter(directions))
            meaning['uncertain']=any(m.get('uncertain') for m in matched_intents)
        elif any(n in spec.request.query for n in ('没有','未见','不伴','否认','并非')):
            meaning['polarity']='negated';meaning['uncertain']=True
        simple=meaning['polarity']=='affirmed' and meaning['direction'] in ('unspecified','static') and not meaning['uncertain']
        expansions=[(cid,normalize(concepts[cid].name)) for cid in selected] if simple else []
        option_lookup={o.entry_id:o for o in taxonomy.options if o.status=='active'} if taxonomy else {}
        filters=spec.filters
        technical_filters=bool(filters.media_kinds or filters.format_names or any(v is not None for v in (
            filters.min_file_duration_seconds,filters.max_file_duration_seconds,filters.min_display_width,
            filters.min_display_height,filters.has_audio,filters.has_transparent_pixels)))
        visual_query=None;visual_state='not_requested'
        if spec.include_drafts and spec.include_visual_drafts and not spec.literal_only:
            visual_state='not_applicable_context' if not simple else 'unavailable'
            if simple and self.visual_encoder:
                try:
                    visual_query=self.visual_encoder.encode(spec.request.query);visual_state='ready_uncalibrated'
                except (ValueError,OSError,RuntimeError,TimeoutError):pass
                except subprocess.SubprocessError:pass
        results=[]
        with self.database.transaction() as c:
            rows=self._visible(c,principal,spec)
            for asset in rows:
                document=self._document(c,asset,versions['taxonomy_release_id'] if versions else None);reasons=[]
                if asset['asset_id'] in filters.exclude_asset_ids:continue
                technical=self.files._technical(c,asset) if technical_filters else None
                if not passes(self,c,asset,document,technical or {},filters):continue
                context=spec.request.context
                if any(ids and not any(oid in option_lookup and any(f['field']==field and option_lookup[oid].value in f['values']
                    for f in document['medical_fields']) for oid in ids) for field,ids in (('科室',context.departments),('器官/系统',context.organs))):continue
                names=[asset['name'],asset['asset_id'],*document['aliases']]
                for value in names:
                    if query in normalize(value):reasons.append({'kind':'literal_name_or_id','score':100,'medical_evidence':False});break
                for field in document['fields']:
                    if any(query in normalize(value) for value in field['values']):reasons.append({'kind':'human_text','field':field['field'],'score':90,'medical_evidence':field['field'] in MEDICAL})
                for cid,term in expansions:
                    if any(term in normalize(value) for value in names):reasons.append({'kind':'expanded_name','concept_id':cid,'score':60,'medical_evidence':False})
                    for field in document['fields']:
                        if any(term in normalize(value) for value in field['values']):reasons.append({'kind':'expanded_human_text','concept_id':cid,'field':field['field'],'score':80,'medical_evidence':False})
                for cid in selected:
                    concept=concepts[cid]
                    classified=bool(simple and concept.option_ids and all(oid in option_lookup and any(f['field']==option_lookup[oid].field and option_lookup[oid].value in f['values'] for f in document['medical_fields']) for oid in concept.option_ids))
                    if classified:
                        for oid in concept.option_ids:
                            option=option_lookup.get(oid)
                            if option and any(f['field']==option.field and option.value in f['values'] for f in document['medical_fields']):
                                reasons.append({'kind':'human_classification','concept_id':cid,'field':option.field,'score':95,'medical_evidence':True})
                    for link in c.execute('SELECT * FROM asset_concept_links WHERE asset_id=? AND concept_id=? AND active=1',(asset['asset_id'],cid)):
                        if not self.links.current(c,link,concept,asset['blob_sha256']):continue
                        if link['polarity']!=meaning['polarity'] or meaning['direction']!='unspecified' and link['direction']!=meaning['direction']:continue
                        if meaning['uncertain']:continue
                        reasons.append({'kind':'human_concept','concept_id':cid,'link_id':link['link_id'],'score':98,'polarity':link['polarity'],
                            'direction':link['direction'],'start_seconds':link['start_seconds'],'end_seconds':link['end_seconds'],
                            'medical_evidence':json.loads(link['payload_json']).get('medical_concept',False)})
                if spec.include_drafts:
                    projected=self.drafts.sync(c,asset,persist=False)
                    for draft in [*projected['labels'],*self._drafts(c,asset,lexicon)]:
                        if query in normalize(draft['text']) or simple and (draft.get('concept_id') in selected or any(term in normalize(draft['text']) for _,term in expansions)):
                            reasons.append(draft|{'draft_kind':draft['kind'],'kind':'ai_draft','score':30,'medical_evidence':False})
                    if visual_query is not None:
                        candidates=[f for f in projected['features'] if f['model_revision']==self.visual_encoder.registry.revision]
                        if candidates:
                            best=max(candidates,key=lambda f:sum(a*b for a,b in zip(visual_query,f['embedding'])))
                            similarity=max(-1.,min(1.,sum(a*b for a,b in zip(visual_query,best['embedding']))))
                            reasons.append({k:v for k,v in best.items() if k!='embedding'}|{'kind':'ai_visual_draft','score':10,
                                'visual_similarity':similarity,'score_kind':'cosine_not_probability','medical_evidence':False,
                                'calibration_state':'not_calibrated','field':None})
                if (context.polarity!='unspecified' or context.direction!='unspecified') and not any(r['kind']=='human_concept' for r in reasons):continue
                if not reasons:continue
                if technical is None:technical=self.files._technical(c,asset)
                score=max(r['score'] for r in reasons)
                status=index_status(c,asset,getattr(self.catalog,'review_policy',None))
                if spec.prefer_confirmed:
                    if status=='confirmed':score+=300
                    elif any(r['kind'] in ('human_text','human_classification','human_concept') for r in reasons):score+=200
                results.append({'root_asset_id':asset['root_asset_id'],'score':score,'asset':{'asset_id':asset['asset_id'],'name':asset['name'],
                    'sha256':asset['blob_sha256'],'revision':asset['revision'],'kind':asset['kind'],'mine':asset['owner_id']==principal.actor_id,
                    'file_access':'authorized','analysis':self.library._analysis(c,asset['asset_id']),'technical':technical,
                    'index_status':status,'ranking_score':score,'reasons':reasons}})
            visual_order=sorted((r for r in results if any(v['kind']=='ai_visual_draft' for v in r['asset']['reasons'])),
                key=lambda r:(-max(v['visual_similarity'] for v in r['asset']['reasons'] if v['kind']=='ai_visual_draft'),r['asset']['asset_id']))
            visual_rank={r['asset']['asset_id']:rank for rank,r in enumerate(visual_order[:10])}
            kept=[]
            for r in results:
                reasons=r['asset']['reasons'];rank=visual_rank.get(r['asset']['asset_id'])
                reasons[:]=[v for v in reasons if v['kind']!='ai_visual_draft' or rank is not None]
                for reason in reasons:
                    if reason['kind']=='ai_visual_draft':reason['score']=20-rank
                if not reasons:continue
                bonus=(300 if spec.prefer_confirmed and r['asset']['index_status']=='confirmed' else 200 if spec.prefer_confirmed and any(v['kind'] in ('human_text','human_classification','human_concept') for v in reasons) else 0)
                r['score']=r['asset']['ranking_score']=max(v['score'] for v in reasons)+bonus;kept.append(r)
            results=kept
            grouped={}
            for result in results:
                root=result['root_asset_id'];group=grouped.setdefault(root,{'root_asset_id':root,'score':0,'versions':[]})
                group['score']=max(group['score'],result['score']);group['versions'].append(result['asset'])
            dataset=fingerprint({'eligible':[(r['asset_id'],r['revision'],r['blob_sha256']) for r in rows],
                'matches':[{'asset_id':r['asset']['asset_id'],'root':r['root_asset_id'],'score':r['score'],'reasons':r['asset']['reasons']} for r in results]})
            scope_versions={key:value for key,value in (versions or {}).items() if key not in ('published_generation','vocabulary_generation')}
            scope=fingerprint({'actor':principal.actor_id,'role':principal.role,'request':spec.model_dump(mode='json',exclude={'cursor'}),'versions':scope_versions,'dataset':dataset})
            after=self._after(spec.cursor,scope)
            for group in grouped.values():group['versions'].sort(key=lambda asset:(asset['ranking_score'],asset['asset_id']))
            ordered=sorted(grouped.values(),key=lambda group:(-group['score'],group['root_asset_id']))
            if after:ordered=[g for g in ordered if (-g['score'],g['root_asset_id'])>after]
            page=ordered[:spec.limit];next_cursor=self._cursor(scope,[-page[-1]['score'],page[-1]['root_asset_id']]) if len(ordered)>spec.limit else None
        recognized=bool(selected)
        empty_reason=None if page else 'recognized_no_visible_assets' if recognized else 'choose_concept' if resolution['candidates'] else 'terminology_unavailable' if resolution['state']=='degraded' else 'unrecognized_expression'
        return {'items':page,'next':next_cursor,'resolution':resolution,'selected_concept_ids':sorted(selected),'intent':meaning,'empty_reason':empty_reason,
            'search_kind':'literal_and_versioned_concepts','include_drafts':spec.include_drafts,'index_revision':dataset,'draft_trial':trial is not None,
            'medical_confirmation':False,'license_confirmation':False,'visual_drafts':{'state':visual_state,'top_k':10,'calibration_state':'not_calibrated'}}
