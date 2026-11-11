"""Authenticated status and explicit repair; no caller-supplied approval or owner."""

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from media_tag.contracts.models import Identifier

from .service import AnalysisService
from .repair import RepairSubmission, repair


def install(app, router, identity, options=None, *, body):
    service = AnalysisService(router, options)
    app.state.analysis = service

    @app.get("/analysis/assets/{asset_id}")
    def status(asset_id: Identifier, principal=Depends(identity)):
        return service.status(asset_id, principal=principal)

    @app.post('/analysis/assets/{asset_id}/repair')
    async def repair_analysis(asset_id: Identifier, request: Request, principal=Depends(identity)):
        spec = await body(request, RepairSubmission)
        return await run_in_threadpool(repair, service, asset_id, spec, principal=principal)
