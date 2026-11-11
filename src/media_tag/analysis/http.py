"""Authenticated status and explicit repair; no caller-supplied approval or owner."""

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from media_tag.contracts.models import Identifier

from .service import AnalysisService
from .repair import RepairSubmission, repair


def install(app, router, identity, options=None, *, body, precision_options=None,audio_topics_options=None):
    from media_tag.precision.contracts import PrecisionDeployment
    deployment = precision_options or PrecisionDeployment()
    audio_deployment = audio_topics_options or PrecisionDeployment()
    service = AnalysisService(router, options, precision_deployment=deployment,audio_deployment=audio_deployment)
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

    from media_tag.audio_topics.service import request_audio_topics
    @app.post('/analysis/assets/{asset_id}/audio-topics')
    async def audio_topics_analysis(asset_id: Identifier, request: Request, principal=Depends(identity)):
        spec = await body(request, PrecisionSubmission)
        return await run_in_threadpool(request_audio_topics, service, audio_deployment, asset_id, spec, principal=principal)
