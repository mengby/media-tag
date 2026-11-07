"""Small authenticated control plane. No static files, publishing or role-bearing requests."""

from hashlib import sha256
import re

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from media_tag.catalog import AccessDenied, CatalogError, NotFound, Principal
from media_tag.contracts.models import ContractModel, Identifier, PositiveInt, WorkerResult
from media_tag.router.models import JobSpec, StepSpec
from media_tag.router.service import Router
from media_tag.storage import IntegrityError, StorageError
from media_tag.upload.models import UploadSpec
from media_tag.variants.models import PlanSpec
from media_tag.workers.probe import MediaError


class Submission(ContractModel):
    request_id: Identifier
    spec: JobSpec


class Attempt(ContractModel):
    task_id: Identifier
    lease_epoch: PositiveInt


class Pause(ContractModel):
    paused: bool


class Replan(ContractModel):
    expected_revision: PositiveInt
    steps: tuple[StepSpec, ...]


class VariantSubmission(ContractModel):
    request_id: Identifier
    spec: PlanSpec


def create_app(router: Router, credentials: dict[str, Principal], *, max_body_bytes=1024 * 1024, uploads=None, variants=None) -> FastAPI:
    if not credentials or any(re.fullmatch(r"[0-9a-f]{64}", digest) is None for digest in credentials):
        raise ValueError("configure SHA256 bearer digests mapped to trusted principals")
    identities = dict(credentials)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    def identity(request: Request) -> Principal:
        auth = request.headers.get("authorization", "")
        scheme, _, token = auth.partition(" ")
        principal = identities.get(sha256(token.encode()).hexdigest()) if scheme.lower() == "bearer" and token else None
        if principal is None:
            raise HTTPException(401, "authentication required", headers={"WWW-Authenticate": "Bearer"})
        return principal

    async def body(request, model):
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > max_body_bytes:
                raise HTTPException(413, "request body limit exceeded")
        try:
            return model.model_validate_json(data)
        except ValidationError as exc:
            # Pydantic's default input/context can echo credentials or arbitrary worker logs.
            return_errors = [{"loc": list(e["loc"]), "type": e["type"]} for e in exc.errors()]
            raise HTTPException(422, return_errors) from None

    @app.exception_handler(CatalogError)
    async def catalogue_error(request, exc):
        status = 403 if isinstance(exc, AccessDenied) else 404 if isinstance(exc, NotFound) else 409
        return JSONResponse({"detail": "permission denied" if status == 403 else "unavailable" if status == 404 else "operation conflicted"}, status_code=status)

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        return JSONResponse({"detail": "invalid operation or configuration"}, status_code=422)

    @app.exception_handler(RequestValidationError)
    async def request_error(request, exc):
        return JSONResponse({'detail': [{'loc': list(e['loc']), 'type': e['type']} for e in exc.errors()]}, status_code=422)

    @app.exception_handler(StorageError)
    @app.exception_handler(OSError)
    async def storage_error(request, exc):
        return JSONResponse({'detail': 'integrity check failed' if isinstance(exc, IntegrityError) else 'storage unavailable'},
                            status_code=422 if isinstance(exc, IntegrityError) else 503)

    @app.exception_handler(MediaError)
    async def media_error(request, exc):
        return JSONResponse({'detail': 'media validation failed', 'code': exc.code}, status_code=422)

    if uploads is not None:
        @app.post('/uploads')
        async def create_upload(request: Request, principal=Depends(identity)):
            data = await body(request, UploadSpec)
            return await run_in_threadpool(uploads.create, data, principal=principal)

        @app.get('/uploads/{upload_id}')
        def upload_status(upload_id: str, principal=Depends(identity)):
            return uploads.get(upload_id, principal=principal)

        @app.put('/uploads/{upload_id}/chunks')
        async def upload_chunk(upload_id: str, offset: int, request: Request, principal=Depends(identity)):
            if not await run_in_threadpool(uploads.check_writable, upload_id, principal=principal):
                raise HTTPException(409, 'upload is closed')
            digest = request.headers.get('x-chunk-sha256', '')
            if re.fullmatch(r'[0-9a-f]{64}', digest) is None:
                raise HTTPException(422, 'chunk SHA256 required')
            data = bytearray()
            async for chunk in request.stream():
                if len(data) + len(chunk) > uploads.options.chunk_bytes:
                    raise HTTPException(413, 'chunk body limit exceeded')
                data.extend(chunk)
            return await run_in_threadpool(uploads.append, upload_id, offset, bytes(data), digest, principal=principal)

        @app.post('/uploads/{upload_id}/complete')
        def complete_upload(upload_id: str, principal=Depends(identity)):
            return uploads.complete(upload_id, principal=principal)

        @app.post('/uploads/{upload_id}/cancel')
        def cancel_upload(upload_id: str, principal=Depends(identity)):
            uploads.cancel(upload_id, principal=principal)
            return {'cancelled': True}

    if variants is not None:
        @app.post('/variants/plans')
        async def plan_variant(request: Request, principal=Depends(identity)):
            data = await body(request, VariantSubmission)
            return await run_in_threadpool(variants.create_plan, data.spec, request_id=data.request_id, principal=principal)

        @app.get('/variants/plans/{plan_id}')
        def variant_plan(plan_id: str, principal=Depends(identity)):
            return variants.get_plan(plan_id, principal=principal)

        @app.get('/assets/{asset_id}/variants')
        def variant_list(asset_id: str, principal=Depends(identity)):
            return variants.list_versions(asset_id, principal=principal)

    @app.post("/jobs")
    async def submit(request: Request, principal=Depends(identity)):
        data = await body(request, Submission)
        return await run_in_threadpool(router.submit, data.spec, request_id=data.request_id, principal=principal)

    @app.get("/jobs/{job_id}")
    def status(job_id: str, principal=Depends(identity)):
        return router.get_job(job_id, principal=principal)

    @app.post("/jobs/{job_id}/pause")
    async def pause(job_id: str, request: Request, principal=Depends(identity)):
        data = await body(request, Pause)
        await run_in_threadpool(router.pause, job_id, paused=data.paused, principal=principal)
        return {"paused": data.paused}

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str, principal=Depends(identity)):
        router.cancel(job_id, principal=principal)
        return {"cancel_requested": True}

    @app.post("/jobs/{job_id}/replan")
    async def replan(job_id: str, request: Request, principal=Depends(identity)):
        data = await body(request, Replan)
        return await run_in_threadpool(router.replan, job_id, data.steps, expected_revision=data.expected_revision, principal=principal)

    @app.post("/worker/claim")
    def claim(principal=Depends(identity)):
        lease = router.claim(principal=principal)
        return None if lease is None else lease.model_dump(mode="json")

    @app.post("/worker/heartbeat")
    async def heartbeat(request: Request, principal=Depends(identity)):
        data = await body(request, Attempt)
        return await run_in_threadpool(router.heartbeat, data.task_id, data.lease_epoch, principal=principal)

    @app.post("/worker/result")
    async def result(request: Request, principal=Depends(identity)):
        data = await body(request, WorkerResult)
        return await run_in_threadpool(router.complete, data, principal=principal)

    @app.post("/worker/release")
    async def release(request: Request, principal=Depends(identity)):
        data = await body(request, Attempt)
        released = await run_in_threadpool(router.release, data.task_id, data.lease_epoch, principal=principal)
        return {"released": released}

    @app.post("/admin/dispatch")
    async def dispatch(request: Request, principal=Depends(identity)):
        data = await body(request, Pause)
        await run_in_threadpool(router.pause_dispatch, paused=data.paused, principal=principal)
        return {"paused": data.paused}

    @app.post("/admin/recover")
    def recover(principal=Depends(identity)):
        router.recover(principal=principal)
        return {"recovered": True}

    return app
