"""Authenticated D06 endpoints, sharing the control plane's body and identity policies."""
from fastapi import Depends,Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from media_tag.catalog import AccessDenied,NotFound
from media_tag.contracts.models import Identifier,Sha256
from media_tag.export.models import ExportSubmission
from media_tag.export.service import ExportService
from media_tag.storage import IntegrityError
from .models import Observation,FieldDecision,Confirmation,GapSubmission,GapRevision
from .policy import index_status


def install(app,reviews,identity,body):
    exports=ExportService(reviews)
    app.state.reviews=reviews;app.state.exports=exports

    @app.get('/review/queue')
    def queue(principal=Depends(identity)):
        from media_tag.catalog.service import _human
        _human(principal)
        with reviews.database.transaction() as connection:
            rows=connection.execute('''SELECT a.* FROM assets a WHERE deleted_at IS NULL AND
                (? OR owner_id=? OR EXISTS(SELECT 1 FROM asset_permissions p WHERE p.asset_id=a.asset_id
                    AND p.subject_id=? AND p.permission='edit')) ORDER BY created_at DESC,asset_id LIMIT 200''',
                (int(principal.role=='admin'),principal.actor_id,principal.actor_id)).fetchall()
            return {'items':[{'asset_id':r['asset_id'],'sha256':r['blob_sha256'],'revision':r['revision'],'name':r['name'],
                'index_status':index_status(connection,r,reviews.policy),'file_access':reviews.files._gate(connection,r) or 'authorized'} for r in rows],
                'medical_reviewer':principal.actor_id in reviews.config.reviewers,'limit':200}

    @app.get('/review/assets/{asset_id}')
    def detail(asset_id:Identifier,principal=Depends(identity)):
        return reviews.detail(asset_id,principal=principal)|{'media':reviews.files.details(asset_id,principal=principal)}

    @app.post('/review/assets/{asset_id}/observations')
    async def observation(asset_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,Observation)
        return await run_in_threadpool(reviews.observe,asset_id,spec,principal=principal)

    @app.post('/review/assets/{asset_id}/fields')
    async def decide(asset_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,FieldDecision)
        return await run_in_threadpool(reviews.decide,asset_id,spec,principal=principal)

    @app.post('/review/assets/{asset_id}/confirmation')
    async def confirm(asset_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,Confirmation)
        return await run_in_threadpool(reviews.confirm,asset_id,spec,principal=principal)

    @app.get('/review/assets/{asset_id}/evidence/{run_id}/{evidence_id}')
    def evidence(asset_id:Identifier,run_id:Identifier,evidence_id:Identifier,sha256:Sha256,principal=Depends(identity)):
        with reviews.database.transaction() as connection:
            asset=reviews._asset(connection,asset_id,principal)
            rows=connection.execute('SELECT suggestion_id FROM tag_suggestions WHERE asset_id=? AND run_id=? ORDER BY suggestion_id',(asset_id,run_id)).fetchall()
            if not rows:raise NotFound('evidence unavailable')
            from media_tag.catalog import RevisionConflict
            from media_tag.models.contracts import ModelFailure
            from media_tag.storage import StorageError
            ref=None;unavailable=None;chosen=None
            # A run can still contain current fields after a newer narrow scope
            # supersedes its first suggestion. Select evidence through any exact,
            # currently qualified field rather than an arbitrary first row.
            for row in rows:
                try:
                    suggestion,_,result=reviews.suggestion(connection,asset,run_id,row[0])
                except (RevisionConflict,ValueError,StorageError,OSError,ModelFailure) as exc:
                    unavailable=exc;continue
                if evidence_id in suggestion.evidence_ids:
                    ref=next((e.artifact for e in result.evidence if e.evidence_id==evidence_id),None)
                    if ref is not None:
                        from .qualification import metadata
                        chosen=(row[0],metadata(reviews,connection,asset,run_id,row[0]).binding)
                        break
            if ref is None:
                if unavailable is not None:raise unavailable
                raise NotFound('evidence unavailable')
            if ref.sha256!=sha256:raise IntegrityError('evidence hash mismatch')
        reviews.files.authorize(asset_id,asset['blob_sha256'],principal=principal,for_bytes=True)
        from hashlib import sha256 as digest
        with reviews.router.files.open_read(ref.ref) as stream:raw=stream.read(32*1024**2+1)
        if len(raw)>32*1024**2 or digest(raw).hexdigest()!=ref.sha256:raise IntegrityError('evidence changed')
        reviews.files.authorize(asset_id,asset['blob_sha256'],principal=principal,for_bytes=True)
        with reviews.database.transaction() as connection:
            latest=reviews._asset(connection,asset_id,principal,asset['blob_sha256'])
            if metadata(reviews,connection,latest,run_id,chosen[0]).binding!=chosen[1]:
                raise RevisionConflict('review evidence producer changed while reading')
        png=raw.startswith(b'\x89PNG\r\n\x1a\n')
        return Response(raw,media_type='image/png' if png else 'application/octet-stream',
            headers={} if png else {'Content-Disposition':'attachment; filename="evidence.bin"'})

    @app.get('/review/gaps')
    def gaps(principal=Depends(identity)):
        return reviews.gaps(principal=principal)

    @app.post('/review/gaps')
    async def create_gap(request:Request,principal=Depends(identity)):
        spec=await body(request,GapSubmission)
        return await run_in_threadpool(reviews.gap_create,spec,principal=principal)

    @app.post('/review/gaps/{gap_id}')
    async def revise_gap(gap_id:Identifier,request:Request,principal=Depends(identity)):
        spec=await body(request,GapRevision)
        return await run_in_threadpool(reviews.gap_revise,gap_id,spec,principal=principal)

    @app.post('/review/exports')
    async def create_export(request:Request,principal=Depends(identity)):
        spec=await body(request,ExportSubmission)
        return await run_in_threadpool(exports.create,spec,principal=principal)

    @app.get('/review/exports/latest')
    def latest_export(principal=Depends(identity)):
        with reviews.database.transaction() as connection:
            row=connection.execute('SELECT export_id FROM csv_export_heads WHERE owner_id=?',(principal.actor_id,)).fetchone()
        if not row:raise NotFound('no published export')
        return exports.get(row[0],principal=principal)

    @app.get('/review/exports/{export_id}')
    def export_status(export_id:Identifier,principal=Depends(identity)):
        return exports.get(export_id,principal=principal)

    @app.get('/review/exports/{export_id}/content/{name}')
    def export_content(export_id:Identifier,name:str,principal=Depends(identity)):
        raw=exports.content(export_id,name,principal=principal)
        return Response(raw,media_type='text/csv' if name.endswith('.csv') else 'application/json',
            headers={'Content-Disposition':f'attachment; filename="{export_id}-{name}"'})
