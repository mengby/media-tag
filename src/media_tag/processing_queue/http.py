"""Authenticated bounded processing requests; private artifacts stay private."""
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from media_tag.contracts.models import Identifier
from .models import CleaningSubmission, ImageSubmission


def install(app, service, identity, body):
    app.state.processing = service

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
