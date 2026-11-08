"""D13 authenticated service API; capability bindings come only from local config."""
from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from media_tag.contracts.models import Identifier
from media_tag.terminology.contracts import CandidateSubmission,CandidateRevision,Review,Publish,Rollback,ImportRows
from .service import PublicationBlocked


def install(app,service,identity,body):
    app.state.terminology=service

    @app.exception_handler(PublicationBlocked)
    async def blocked(request,exc):
        return JSONResponse({'error':'publication_blocked','issues':exc.problems},status_code=422)

    @app.get('/terminology/active')
    def active(principal=Depends(identity)):return service.active(principal=principal)

    @app.get('/terminology/candidates')
    def candidates(principal=Depends(identity)):return service.list_candidates(principal=principal)

    @app.post('/terminology/candidates')
    async def submit(request:Request,principal=Depends(identity)):
        spec=await body(request,CandidateSubmission)
        return await run_in_threadpool(service.submit,spec,principal=principal)

    @app.get('/terminology/candidates/{candidate_id}')
    def detail(candidate_id:Identifier,principal=Depends(identity)):return service.get(candidate_id,principal=principal)

    @app.post('/terminology/candidates/{candidate_id}/revision')
    async def revise(candidate_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,CandidateRevision)
        return await run_in_threadpool(service.revise,candidate_id,spec,principal=principal)

    @app.post('/terminology/candidates/{candidate_id}/review')
    async def review(candidate_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,Review)
        return await run_in_threadpool(service.review,candidate_id,spec,principal=principal)

    @app.post('/terminology/candidates/{candidate_id}/import')
    async def import_rows(candidate_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,ImportRows)
        return await run_in_threadpool(service.import_rows,candidate_id,spec,principal=principal)

    @app.get('/terminology/candidates/{candidate_id}/preview')
    def preview(candidate_id:Identifier,principal=Depends(identity)):return service.preview(candidate_id,principal=principal)

    @app.post('/terminology/candidates/{candidate_id}/publish')
    async def publish(candidate_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,Publish)
        return await run_in_threadpool(service.publish,candidate_id,spec,principal=principal)

    @app.get('/terminology/releases/{release_id}')
    def release(release_id:Identifier,principal=Depends(identity)):return service.release(release_id,principal=principal)

    @app.post('/terminology/rollback')
    async def rollback(request:Request,principal=Depends(identity)):
        spec=await body(request,Rollback)
        return await run_in_threadpool(service.rollback,spec,principal=principal)
