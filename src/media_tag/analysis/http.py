"""Authenticated analysis intent inspection; no caller-supplied approval or owner."""

from fastapi import Depends

from media_tag.contracts.models import Identifier

from .service import AnalysisService


def install(app, router, identity):
    service = AnalysisService(router)
    app.state.analysis = service

    @app.get("/analysis/assets/{asset_id}")
    def status(asset_id: Identifier, principal=Depends(identity)):
        return service.status(asset_id, principal=principal)
