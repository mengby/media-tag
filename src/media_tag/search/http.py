"""D15 authenticated retrieval/feedback and explicit management capabilities."""
import json
from typing import Literal
from fastapi import Depends,Request,HTTPException
from starlette.concurrency import run_in_threadpool
from media_tag.catalog import AccessDenied,RevisionConflict
from media_tag.contracts.models import Identifier,FieldValue,ContractModel
from media_tag.review.models import GapSubmission
from media_tag.terminology.lexicon.service import parse_payload
from .models import SearchRequest,ConceptLink,RevokeLink,Feedback,FeedbackRevision,FeedbackDecision,ImportMapping,DraftTrial


def install(app,search,identity,body):
    app.state.search=search;terms=search.terminology

    @app.post('/search/query')
    async def query(request:Request,principal=Depends(identity)):
        spec=await body(request,SearchRequest)
        return await run_in_threadpool(search.search,spec,principal=principal)

    @app.get('/search/vocabulary')
    def vocabulary(principal=Depends(identity)):return search.vocabulary(principal=principal)

    @app.get('/search/assets/{asset_id}/concepts')
    def concepts(asset_id:Identifier,principal=Depends(identity)):return search.links.detail(asset_id,principal=principal)

    @app.post('/search/assets/{asset_id}/concepts')
    async def create_link(asset_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,ConceptLink)
        return await run_in_threadpool(search.links.create,asset_id,spec,principal=principal)

    @app.post('/search/assets/{asset_id}/concepts/{link_id}/revoke')
    async def revoke_link(asset_id:Identifier,link_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,RevokeLink)
        return await run_in_threadpool(search.links.revoke,asset_id,link_id,spec,principal=principal)

    @app.get('/search/feedback')
    def feedbacks(principal=Depends(identity)):return search.feedback.listing(principal=principal)

    @app.post('/search/feedback')
    async def feedback(request:Request,principal=Depends(identity)):
        spec=await body(request,Feedback)
        return await run_in_threadpool(search.feedback.create,spec,principal=principal)

    @app.get('/search/feedback/{feedback_id}')
    def feedback_detail(feedback_id:Identifier,principal=Depends(identity)):return search.feedback.get(feedback_id,principal=principal)

    @app.post('/search/feedback/{feedback_id}/revision')
    async def feedback_revision(feedback_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,FeedbackRevision)
        return await run_in_threadpool(search.feedback.revise,feedback_id,spec,principal=principal)

    @app.post('/search/feedback/{feedback_id}/decision')
    async def feedback_decision(feedback_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,FeedbackDecision)
        return await run_in_threadpool(search.feedback.decide,feedback_id,spec,principal=principal)

    @app.get('/search/management')
    def management(principal=Depends(identity)):
        manager=principal.actor_id in terms.config.managers;publisher=principal.actor_id in terms.config.publishers
        if not manager and not publisher:raise AccessDenied('terminology management capability required')
        from media_tag.catalog.service import _human
        _human(principal)
        with search.database.transaction() as c:
            releases=[dict(r) for r in c.execute('SELECT release_id,kind,version,taxonomy_release_id,published_by,published_at FROM terminology_releases ORDER BY published_at DESC LIMIT 100')]
        from media_tag.review.policy import ReviewPolicy
        from media_tag.review.models import ReviewDeployment
        policy=search.reviews.policy if search.reviews else ReviewPolicy(terms.contract,ReviewDeployment())
        fields=[]
        for name in ('用途大类','科室','器官/系统','疾病/症状','过程/状态'):
            definition=policy.asset_fields.get(name)
            if definition:fields.append({'name':name,'mode':'external' if definition.selection_policy=='external_vocabulary' else 'closed' if definition.field_type in ('单选','多选') else 'process_text',
                'options':list(policy.options(definition) or ()) if definition.selection_policy!='external_vocabulary' else []})
        return {'capabilities':{'manage':manager,'publish':publisher},'candidates':terms.list_candidates(principal=principal)['items'],
            'feedback':search.feedback.listing(principal=principal,management=manager)['items'],'releases':releases,'publication':terms.active(principal=principal),
            'serving':search.resolver.status(principal=principal),'vocabulary':search.vocabulary(principal=principal),
            'template_fingerprint':terms.contract.fingerprint,'taxonomy_fields':fields}

    @app.get('/search/management/candidates/{candidate_id}/diff')
    def diff(candidate_id:Identifier,principal=Depends(identity)):
        if principal.actor_id in terms.config.managers:terms._manager(principal)
        else:terms._manager(principal,publish=True)
        with search.database.transaction() as c:
            row=terms._candidate(c,candidate_id,principal);payload=parse_payload(row['kind'],json.loads(row['payload_json']))
            return {'generation':terms._head(c)['generation'],'revision':row['revision'],'issues':terms._issues(c,payload),'diff':terms._diff(c,row,payload,principal)}

    @app.post('/search/management/candidates/{candidate_id}/trial')
    async def trial(candidate_id:Identifier,request:Request,principal=Depends(identity)):
        terms._manager(principal);spec=await body(request,DraftTrial)
        with search.database.transaction() as c:
            row=terms._candidate(c,candidate_id,principal)
            if row['kind']!='lexicon' or row['revision']!=spec.expected_revision:raise RevisionConflict('current lexicon draft required')
            payload=parse_payload('lexicon',json.loads(row['payload_json']));issues=terms._issues(c,payload)
        live=await run_in_threadpool(search.search,spec.request,principal=principal)
        draft=await run_in_threadpool(search.search,spec.request,principal=principal,trial=(payload,spec.expected_revision))
        return {'published':live,'draft':draft,'publication_issues':issues,'online_changed':False}

    @app.post('/search/imports')
    async def upload_import(request:Request,format:Literal['csv','xlsx'],principal=Depends(identity)):
        terms._manager(principal);raw=bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>4*1024**2:raise HTTPException(413,'import byte budget exceeded')
        return await run_in_threadpool(search.imports.upload,bytes(raw),format,principal=principal)

    @app.get('/search/imports/{import_id}')
    def inspect_import(import_id:Identifier,sheet:str|None=None,principal=Depends(identity)):
        return search.imports.inspect(import_id,principal=principal,sheet=sheet)

    @app.post('/search/imports/{import_id}/preview')
    async def preview_import(import_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,ImportMapping)
        return await run_in_threadpool(search.imports.preview,import_id,spec,principal=principal)

    @app.post('/search/gaps')
    async def create_gap(request:Request,principal=Depends(identity)):
        if search.reviews is None:raise HTTPException(404,'review service unavailable')
        spec=await body(request,Feedback)
        # A query/need is a human-provided request, not a fabricated failed search log.
        submission=GapSubmission(request_id=spec.request_id,values={'需要的画面':FieldValue(state='observed',values=(spec.quote or spec.expression,))},
            note='人工从搜索提交素材需求；不表示已存在该素材。')
        return await run_in_threadpool(search.reviews.gap_create,submission,principal=principal)
