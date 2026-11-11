"""Authenticated bounded processing requests; private artifacts stay private."""
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from media_tag.contracts.models import Identifier,Sha256
from media_tag.files.http import transfer
from .derived import ProcessingDerivedService
from .derived_contracts import OutputReview
from .models import CleaningSubmission, ImageSubmission


def install(app, service, identity, body):
    app.state.processing = service
    derived=ProcessingDerivedService(service.router,service.tools,options=service.options) if service is not None else None
    app.state.processing_derived=derived

    def available():
        if service is None or not service.options.enabled:
            raise HTTPException(503, 'processing_disabled')
        return service

    @app.post('/processing/cleaning')
    async def cleaning(request: Request, principal=Depends(identity)):
        current = available()
        spec = await body(request, CleaningSubmission)
        return await run_in_threadpool(current.request, spec, principal=principal)

    @app.post('/processing/images')
    async def images(request: Request, principal=Depends(identity)):
        current = available()
        spec = await body(request, ImageSubmission)
        return await run_in_threadpool(current.request, spec, principal=principal)

    @app.get('/processing/jobs/{job_id}')
    async def status(job_id: Identifier, principal=Depends(identity)):
        return await run_in_threadpool(available().status, job_id, principal=principal)

    @app.post('/processing/jobs/{job_id}/adopt')
    async def adopt(job_id: Identifier, principal=Depends(identity)):
        available()
        return await run_in_threadpool(derived.adopt,job_id,principal=principal)

    @app.get('/processing/outputs/{asset_id}')
    async def output_status(asset_id: Identifier, principal=Depends(identity)):
        available()
        return await run_in_threadpool(derived.status,asset_id,principal=principal)

    @app.get('/processing/outputs/{asset_id}/comparison')
    async def comparison(asset_id: Identifier, principal=Depends(identity)):
        available()
        return await run_in_threadpool(derived.comparison,asset_id,principal=principal)

    @app.api_route('/processing/outputs/{asset_id}/preview-content',methods=['GET','HEAD'])
    async def preview(asset_id: Identifier,sha256:Sha256,request:Request,principal=Depends(identity)):
        available()
        opened=await run_in_threadpool(derived.review_files().open,asset_id,sha256,principal=principal)
        return transfer(opened,request,inline=True)

    @app.api_route('/processing/outputs/{asset_id}/source-content',methods=['GET','HEAD'])
    async def source_preview(asset_id: Identifier,sha256:Sha256,request:Request,principal=Depends(identity)):
        available()
        opened=await run_in_threadpool(derived.open_source,asset_id,sha256,principal=principal)
        return transfer(opened,request,inline=True)

    @app.post('/processing/outputs/{asset_id}/reviews')
    async def review(asset_id: Identifier,request:Request,principal=Depends(identity)):
        available()
        spec=await body(request,OutputReview)
        return await run_in_threadpool(derived.review,asset_id,spec,principal=principal)
