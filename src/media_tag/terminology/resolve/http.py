"""Interactive queries bypass long asset rendering; no Worker or publication rights."""
from fastapi import Depends,Request
from contextlib import asynccontextmanager
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from media_tag.models.contracts import ModelFailure
from .models import ResolveInput,IndexActivation


def install(app,resolver,identity,body):
    app.state.resolver=resolver
    previous=app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        async with previous(application) as state:
            try:yield state
            finally:await run_in_threadpool(resolver.close)
    app.router.lifespan_context=lifespan

    @app.exception_handler(ModelFailure)
    async def model_error(request,exc):
        return JSONResponse({'error':exc.code,'retryable':exc.code in ('text_model_timeout','text_model_process_failed')},status_code=503)

    @app.get('/terminology/resolve/status')
    def status(principal=Depends(identity)):return resolver.status(principal=principal)

    @app.post('/terminology/resolve')
    async def resolve(request:Request,principal=Depends(identity)):
        spec=await body(request,ResolveInput)
        return await run_in_threadpool(resolver.resolve,spec,principal=principal)

    @app.post('/terminology/text-indices/activate')
    async def activate(request:Request,principal=Depends(identity)):
        spec=await body(request,IndexActivation)
        return await run_in_threadpool(resolver.indices.activate,spec,principal=principal)
