from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from media_tag.contracts.models import Identifier
from .query import CandidateQuery, DescriptorQuery


def install(app, router, identity, body):
    service = DescriptorQuery(router)
    app.state.similarity = service

    @app.get('/similarity/assets/{asset_id}')
    def status(asset_id: Identifier, principal=Depends(identity)):
        return service.status(asset_id, principal=principal)

    @app.post('/similarity/assets/{asset_id}/candidates')
    async def candidates(asset_id: Identifier, request: Request, principal=Depends(identity)):
        spec = await body(request, CandidateQuery)
        return await run_in_threadpool(service.candidates, asset_id, spec, principal=principal)
