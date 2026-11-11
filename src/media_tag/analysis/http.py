"""Authenticated status and explicit repair; no caller-supplied approval or owner."""

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from media_tag.contracts.models import Identifier

from .service import AnalysisService
from .repair import RepairSubmission, repair


def install(app, router, identity, options=None, *, body, precision_options=None):
    from media_tag.precision.contracts import PrecisionDeployment
    deployment = precision_options or PrecisionDeployment()
    service = AnalysisService(router, options, precision_deployment=deployment)
    app.state.analysis = service

    @app.get("/analysis/assets/{asset_id}")
    def status(asset_id: Identifier, principal=Depends(identity)):
        return service.status(asset_id, principal=principal)

    @app.post('/analysis/assets/{asset_id}/repair')
    async def repair_analysis(asset_id: Identifier, request: Request, principal=Depends(identity)):
        spec = await body(request, RepairSubmission)
        return await run_in_threadpool(repair, service, asset_id, spec, principal=principal)

    from media_tag.precision.service import PrecisionSubmission, request_precision
    @app.post('/analysis/assets/{asset_id}/precision')
    async def precision_analysis(asset_id: Identifier, request: Request, principal=Depends(identity)):
        spec = await body(request, PrecisionSubmission)
        return await run_in_threadpool(request_precision, service, deployment, asset_id, spec, principal=principal)
