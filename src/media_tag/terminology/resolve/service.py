"""Read-only resolution from a verified serving tuple; exact phrases survive model failure."""
from collections import OrderedDict
import json
import math
import threading
import time
from media_tag.catalog import AccessDenied,NotFound
from media_tag.models.contracts import ModelFailure
from media_tag.storage import StorageError
from media_tag.terminology.embedding.index import IndexStore,checked_vectors
from media_tag.terminology.embedding.registry import BGERegistry
from media_tag.terminology.embedding.process import ProcessEncoder
from media_tag.terminology.lexicon.service import fingerprint
from .matching import exact_matches,concept_documents,intent,NEGATIVE
from .models import PREPROCESS_VERSION,MATCH_VERSION


class Resolver:
    def __init__(self,terminology,options,*,registry=None,encoder=None):
        if not options.enabled:raise ValueError('resolver is disabled')
        self.terminology,self.options=terminology,options
        self.registry=registry or BGERegistry(options.model_root)
        self.encoder=encoder if encoder is not None else ProcessEncoder(self.registry,terminology.router.workspace) if options.model_enabled else None
        self.indices=IndexStore(terminology,self.registry,self.encoder,options)
        self.cache=OrderedDict();self.lock=threading.Lock()

    def close(self):
        if self.encoder is not None and hasattr(self.encoder,'close'):self.encoder.close()

    def _snapshot(self,*,pinned=None):
        with self.terminology.database.transaction() as c:
            published=self.terminology._head(c);head=self.indices.head(c)
            if pinned is not None:
                taxid,lexid,indexid=pinned.taxonomy_release_id,pinned.lexicon_release_id,pinned.embedding_index_id
            elif head['lexicon_release_id']:
                taxid,lexid,indexid=head['taxonomy_release_id'],head['lexicon_release_id'],head['index_id']
            else:
                taxid,lexid,indexid=published['taxonomy_release_id'],published['lexicon_release_id'],None
            if not taxid or not lexid:raise NotFound('published vocabulary unavailable')
            taxonomy,lexicon,versions=self.indices.pair(c,taxid,lexid)
            documents=concept_documents(lexicon);index=None;index_error=None
            if indexid:
                try:index=self.indices.read(c,indexid,taxonomy_id=taxid,lexicon_id=lexid,documents=documents)
                except (StorageError,ValueError,NotFound):index_error='text_index_unavailable'
            versions.update(taxonomy_release_id=taxid,lexicon_release_id=lexid,embedding_index_id=indexid,
                model_revision=self.registry.revision,preprocess_version=PREPROCESS_VERSION,match_version=MATCH_VERSION,
                resolution_policy_sha256=fingerprint(self.options.model_dump(mode='json',include={'model_enabled','min_cosine','min_margin','query_timeout_seconds','cache_entries'})),
                serving_epoch=head['epoch'] if pinned is None else None,published_generation=published['generation'],
                vocabulary_generation=head['vocabulary_generation'] if pinned is None and head['lexicon_release_id'] else published['generation'])
            return taxonomy,lexicon,index,index_error,versions

    def status(self,*,principal):
        if principal.role not in ('editor','reader','admin'):raise AccessDenied('personnel read capability required')
        with self.terminology.database.transaction() as c:
            published=self.terminology._head(c);head=self.indices.head(c)
        try:
            _,_,index,error,versions=self._snapshot()
            return {'state':'ready' if index and not error else 'lexical_only','serving':head,'published':published,
                'pending_publication':head['vocabulary_generation']!=published['generation'],'versions':versions,
                'model_enabled':self.options.model_enabled,'model_device':'cpu','business_calibrated':False}
        except (StorageError,ValueError,NotFound):
            return {'state':'unavailable','serving':head,'published':published,'reason':'published_vocabulary_unavailable',
                'model_enabled':self.options.model_enabled,'business_calibrated':False}

    def resolve(self,request,*,principal,pinned=None,timeout_seconds=None,stopped=None):
        if principal.role not in ('editor','reader','admin'):raise AccessDenied('personnel read capability required')
        if pinned is None and (request.purpose!='search' or request.evidence_origin!='human_input' or request.evidence_ref is not None):
            raise ValueError('interactive search only accepts human text; source claims require an asset Worker task')
        started=time.monotonic();errors=[]
        base={'query':request.query,'literal_query':request.query,'literal_search_available':True,'purpose':request.purpose,
            'evidence_origin':request.evidence_origin,'medical_evidence_eligible':False,'effective_values_written':False,
            'vocabulary_written':False,'candidates':[],'matches':[],'business_calibrated':False,'cache_hit':False}
        try:taxonomy,lexicon,index,index_error,versions=self._snapshot(pinned=pinned)
        except (StorageError,ValueError,NotFound):
            return base|{'state':'degraded','model_state':'unavailable','errors':['published_vocabulary_unavailable'],
                         'versions':None,'elapsed_seconds':time.monotonic()-started}
        allowed_options={o.entry_id:o for o in taxonomy.options if o.status=='active'}
        for field,label in (('departments','科室'),('organs','器官/系统')):
            if any(oid not in allowed_options or allowed_options[oid].field!=label for oid in getattr(request.context,field)):
                raise ValueError('context must reference the bound published taxonomy')
        key=fingerprint({'actor':principal.actor_id,'role':principal.role,'input':request.model_dump(mode='json'),'versions':versions,
                         'threshold':self.options.min_cosine,'margin':self.options.min_margin,'model_enabled':self.options.model_enabled})
        # Verify files BEFORE a cache hit, so removal/corruption cannot hide behind cache.
        if not index_error:
            with self.lock:
                cached=self.cache.get(key)
                if cached is not None:
                    self.cache.move_to_end(key)
                    return json.loads(cached)|{'cache_hit':True,'elapsed_seconds':time.monotonic()-started}
        matches,match_errors=exact_matches(lexicon,request);errors.extend(match_errors)
        candidates=[m for m in matches if m['context_state']!='excluded']
        base.update(matches=matches,versions=versions,pending_publication=versions['vocabulary_generation']!=versions['published_generation'],
                    index_state='verified' if index is not None else 'invalid' if index_error else 'missing')
        if matches:
            ready=[m for m in candidates if m['context_state']=='matched' and m['default_expand']]
            targets={m['concept_id'] for m in candidates}
            state='resolved' if len(targets)==1 and ready and not errors else 'ambiguous' if candidates else 'no_match'
            result=base|{'state':state,'candidates':candidates[:request.top_k],'model_state':'not_needed','errors':errors,
                         'reason':'exact_published_phrase' if ready else 'context_required_or_non_equivalent' if candidates else 'context_rejected'}
        elif not request.use_model:
            result=base|{'state':'no_match','model_state':'not_requested','errors':errors,'reason':'no_exact_phrase'}
        else:
            code=index_error or ('text_model_disabled' if not self.options.model_enabled or self.encoder is None else 'text_index_missing' if index is None else None)
            if code:
                result=base|{'state':'degraded','model_state':'unavailable','errors':errors+[code],'reason':code}
            else:
                timeout=min(timeout_seconds or self.options.query_timeout_seconds,self.options.query_timeout_seconds)
                timeout-=time.monotonic()-started
                try:
                    if timeout<=0:raise ModelFailure('text_model_timeout')
                    query_vector=checked_vectors(self.encoder.encode([request.query],query=True,timeout_seconds=timeout,stopped=stopped),1)[0]
                    ranked=[]
                    for document,vector in zip(index['documents'],index['vectors']):
                        if request.concept_types and document['concept_type'] not in request.concept_types:continue
                        score=math.fsum(a*b for a,b in zip(query_vector,vector))
                        if score<self.options.min_cosine:continue
                        meaning=intent(request.query,0,len(request.query),request.context)
                        if request.context.polarity=='unspecified' and any(n in request.query for n in NEGATIVE):meaning['polarity']='negated';meaning['uncertain']=True
                        ranked.append({'concept_id':document['concept_id'],'name':document['name'],'concept_type':document['concept_type'],
                            'path':'bge_candidate','relation':'unverified_candidate','score':max(-1.0,min(1.0,score)),
                            'score_kind':'cosine_not_medical_probability','default_expand':False,'context_state':'candidate_only',
                            'intent':meaning,'raw_span':request.query,'start':0,'end':len(request.query)})
                    ranked.sort(key=lambda r:(-r['score'],r['concept_id']))
                    margin=ranked[0]['score']-ranked[1]['score'] if len(ranked)>1 else None
                    result=base|{'state':'ambiguous' if ranked else 'no_match','candidates':ranked[:request.top_k],
                        'model_state':'used','errors':errors,'reason':'model_margin_ambiguous' if margin is not None and margin<self.options.min_margin else 'model_candidate_requires_confirmation' if ranked else 'below_rejection_threshold',
                        'model_margin':margin,'resource_usage':getattr(self.encoder,'stats',{})}
                except ModelFailure as exc:
                    if exc.code=='cancelled':raise
                    result=base|{'state':'degraded','model_state':'timed_out' if exc.code=='text_model_timeout' else 'unavailable',
                        'errors':errors+[exc.code],'reason':exc.code}
                except (StorageError,ValueError,TypeError):
                    result=base|{'state':'degraded','model_state':'unavailable','errors':errors+['text_model_invalid_output'],'reason':'text_model_invalid_output'}
        result['elapsed_seconds']=time.monotonic()-started
        if result['state']!='degraded' and self.options.cache_entries:
            with self.lock:
                self.cache[key]=json.dumps(result,allow_nan=False,ensure_ascii=True)
                self.cache.move_to_end(key)
                while len(self.cache)>self.options.cache_entries:self.cache.popitem(last=False)
        return result
