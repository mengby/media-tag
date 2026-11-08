"""Exact phrases and deliberately conservative context checks, without diagnosis."""
import re
import unicodedata
from media_tag.terminology.contracts import normalize

NEGATIVE=('没有','未见','不伴','否认','并非','未出现','无','不')
INCREASING=('加重','增多','增大','恶化','上升','增加')
DECREASING=('缓解','减轻','消退','减少','下降','改善')
BOUNDARY=re.compile(r'[，,。.;；！？!?、]|但是|但|而')


def normalized_offsets(text):
    """Map normalized characters back to whole raw Unicode clusters, including ligatures."""
    chunks=[]
    for i,char in enumerate(text):
        if unicodedata.combining(char) and chunks:chunks[-1][0]+=char;chunks[-1][2]=i+1
        else:chunks.append([char,i,i+1])
    output=[];positions=[];pending=None
    for chunk,start,end in chunks:
        for char in unicodedata.normalize('NFKC',chunk).casefold():
            if char.isspace():
                if output:pending=(start,end) if pending is None else (pending[0],end)
            else:
                if pending is not None:output.append(' ');positions.append(pending);pending=None
                output.append(char);positions.append((start,end))
    normalized=''.join(output)
    # A rare composition across clusters must not produce a fabricated raw span.
    if normalized!=normalize(text):return normalize(text),None
    return normalized,positions


def intent(query,start,end,explicit):
    boundaries=list(BOUNDARY.finditer(query))
    left=max((m.end() for m in boundaries if m.end()<=start),default=0)
    right=min((m.start() for m in boundaries if m.start()>=end),default=len(query))
    prefix=query[max(left,start-12):start];suffix=query[end:min(right,end+16)]
    local=prefix+query[start:end]+suffix;uncertain=False
    negative=any(n in prefix for n in NEGATIVE) or any(n in suffix for n in ('不存在','未出现','已排除'))
    polarity='negated' if negative else 'affirmed'
    inside=query[start:end]
    if any(n in inside for n in NEGATIVE):
        # An entire alias can contain a negative operator or an idiom such as
        # 不通气. Do not silently erase it by treating the whole phrase as positive.
        uncertain=True
        if any(n in inside for n in ('没有','未见','不伴','否认','并非','未出现')):polarity='negated'
    if explicit.polarity!='unspecified':
        if negative and explicit.polarity!='negated':uncertain=True
        polarity=explicit.polarity
    increasing=any(w in local for w in INCREASING);decreasing=any(w in local for w in DECREASING)
    direction='increasing' if increasing and not decreasing else 'decreasing' if decreasing and not increasing else 'unspecified'
    if increasing and decreasing:uncertain=True
    if any(n in suffix for n in NEGATIVE) and (increasing or decreasing) or any('不'+w in local or '未'+w in local for w in (*INCREASING,*DECREASING)):
        direction='unspecified';uncertain=True
    if any(w in local for w in ('保持稳定','不变')):direction='static'
    if '→' in local or '->' in local:direction='transition';uncertain=True
    if explicit.direction!='unspecified':
        if direction not in ('unspecified',explicit.direction):uncertain=True
        direction=explicit.direction
    return {'polarity':polarity,'direction':direction,'uncertain':uncertain,'raw_clause':query[left:right]}


def context_state(context,request,intention):
    missing=[];excluded=[]
    for field in ('departments','organs'):
        required=set(getattr(context,field));provided=set(getattr(request.context,field))
        if required and not provided:missing.append(field)
        elif required and not required & provided:excluded.append(field)
    for field in ('polarity','direction'):
        expected=getattr(context,field);actual=intention[field]
        if expected!='unspecified' and actual=='unspecified':missing.append(field)
        elif expected!='unspecified' and expected!=actual:excluded.append(field)
    text=normalize(request.query)
    if any(normalize(t) not in text for t in context.required_terms):missing.append('required_terms')
    if any(normalize(t) in text for t in context.excluded_terms):excluded.append('excluded_terms')
    if intention['uncertain']:missing.append('intent')
    return ('excluded' if excluded else 'needs_context' if missing else 'matched'),missing+excluded


def exact_matches(lexicon,request):
    normalized,positions=normalized_offsets(request.query)
    if positions is None:return [],['normalization_span_unavailable']
    concepts={c.entry_id:c for c in lexicon.concepts if c.status=='active'}
    records=[]
    for c in concepts.values():
        records.append((c.name,c.entry_id,'exact_name',None))
        for old in c.old_names:records.append((old,c.entry_id,'old_name',None))
    for alias in lexicon.aliases:records.append((alias.expression,alias.target_id,'exact_alias',alias))
    matches=[]
    for phrase,target,path,alias in records:
        concept=concepts.get(target)
        if concept is None or request.concept_types and concept.concept_type not in request.concept_types:continue
        term=normalize(phrase)
        if not term:continue
        offset=0
        while True:
            index=normalized.find(term,offset)
            if index<0:break
            stop=index+len(term);offset=index+1
            # Avoid ASCII substring matches, while retaining meaningful Chinese nesting.
            if term[0].isascii() and term[0].isalnum() and index and normalized[index-1].isascii() and normalized[index-1].isalnum():continue
            if term[-1].isascii() and term[-1].isalnum() and stop<len(normalized) and normalized[stop].isascii() and normalized[stop].isalnum():continue
            start,end=positions[index][0],positions[stop-1][1]
            meaning=intent(request.query,start,end,request.context)
            state,reasons=context_state(alias.context,request,meaning) if alias else ('needs_context' if meaning['uncertain'] else 'matched',[])
            default=bool(state=='matched' and (alias is None or alias.relation=='equivalent' and alias.default_expand))
            # Structured negatives/directions remain visible, but are not positive expansion.
            if meaning['polarity']=='negated' or meaning['direction'] not in ('unspecified','static') and (alias is None or alias.context.direction=='unspecified'):
                default=False
            matches.append({'concept_id':target,'name':concept.name,'concept_type':concept.concept_type,'path':path,
                'entry_id':alias.entry_id if alias else concept.entry_id,'relation':alias.relation if alias else 'equivalent',
                'raw_span':request.query[start:end],'start':start,'end':end,'context_state':state,'context_reasons':reasons,
                'intent':meaning,'default_expand':default,'score':None,'score_kind':None})
            if len(matches)>=200:return matches,['phrase_match_budget']
    matches.sort(key=lambda m:(m['start'],-(m['end']-m['start']),m['concept_id'],m['entry_id']))
    return matches,[]


def concept_documents(lexicon):
    """A reproducible text corpus, with published aliases and scope, separate from CLIP."""
    result=[]
    for concept in lexicon.concepts:
        if concept.status!='active':continue
        aliases=[a for a in lexicon.aliases if a.target_id==concept.entry_id]
        document={'concept_id':concept.entry_id,'name':concept.name,'concept_type':concept.concept_type,
            'definition':concept.definition,'scope':concept.scope,'old_names':list(concept.old_names),
            'aliases':[{'expression':a.expression,'relation':a.relation,'context':a.context.model_dump(mode='json'),
                        'default_expand':a.default_expand} for a in aliases]}
        # Non-equivalent mappings may rank as candidates but never become equivalent.
        text='\n'.join([concept.name,concept.definition,concept.scope,*concept.old_names,
                         *(a.expression for a in aliases if a.relation=='equivalent' and a.default_expand)])
        document['text']=text
        result.append(document)
    return sorted(result,key=lambda r:r['concept_id'])
