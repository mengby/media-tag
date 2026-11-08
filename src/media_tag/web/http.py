"""Same-origin personnel sessions; native media Range requests retain per-object authorization."""
from hashlib import sha256
from importlib.resources import files
import json
import secrets
import threading
import time
from typing import Literal

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool

from media_tag.catalog import Principal, AccessDenied, RevisionConflict
from media_tag.catalog.service import _human
from media_tag.contracts.models import Identifier, Sha256
from media_tag.files.http import transfer
from media_tag.files.service import FileError
from media_tag.storage import IntegrityError
from media_tag.transforms.models import RevisionSubmission
from .models import Enrollment, Retry, ReviewAssignment
from .service import LibraryService

COOKIE = 'media_tag_session'
SESSION_SECONDS = 8 * 3600


class Sessions:
    def __init__(self):
        self.items = {}
        self.lock = threading.Lock()

    def create(self, principal):
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.monotonic()
            self.items = {key: value for key, value in self.items.items() if value[0] > now}
            if len(self.items) >= 1000:
                raise HTTPException(503, 'session capacity reached')
            self.items[sha256(token.encode()).hexdigest()] = (now + SESSION_SECONDS, principal)
        return token

    def principal(self, request):
        token = request.cookies.get(COOKIE, '')
        with self.lock:
            value = self.items.get(sha256(token.encode()).hexdigest())
        return value[1] if value and value[0] > time.monotonic() else None

    def remove(self, request):
        with self.lock:
            self.items.pop(sha256(request.cookies.get(COOKIE, '').encode()).hexdigest(), None)


def install(app, router, identity, body, credentials, *, uploads, redactions, retiming, reviews=None,search=None,local_browser_login=None):
    sessions = Sessions()
    app.state.ui_sessions = sessions
    from .local_login import TicketStore,TicketInput
    local_tickets=TicketStore(local_browser_login) if local_browser_login else None
    people = sorted({p.actor_id for p in credentials.values() if p.role in ('editor','reader','admin')})
    editors = {p.actor_id for p in credentials.values() if p.role in ('editor','admin')}
    library = LibraryService(router, people=people,probe_max_pixels=uploads.options.probe_max_pixels if uploads else 16*1024**2)
    app.state.library = library
    library.dispatch_pending(principal=Principal(actor_id='local-library-recovery', role='admin'))

    @app.middleware('http')
    async def browser_policy(request, call_next):
        if request.method not in ('GET','HEAD','OPTIONS') and (request.url.path.startswith('/ui/') or request.cookies.get(COOKIE)):
            origin = request.headers.get('origin')
            expected = f'{request.url.scheme}://{request.url.netloc}'
            if (origin is not None and origin != expected or request.headers.get('sec-fetch-site') == 'cross-site'
                    or (request.cookies.get(COOKIE) and not request.headers.get('authorization') and origin != expected)):
                return Response('same-origin request required', status_code=403, headers={'Cache-Control':'no-store'})
        response = await call_next(request)
        if request.url.path.startswith('/ui') or request.cookies.get(COOKIE):
            response.headers.update({'Cache-Control':'private, no-store','Vary':'Cookie, Authorization',
                'Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
                'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"})
        return response

    for url, name, mime in [('/ui/','index.html','text/html'),('/ui/app.js','app.js','text/javascript'),
                            ('/ui/sha256.js','sha256.js','text/javascript'),('/ui/review.js','review.js','text/javascript'),
                            ('/ui/search.js','search.js','text/javascript'),('/ui/terms.js','terms.js','text/javascript'),('/ui/style.css','style.css','text/css')]:
        def make_resource(name, mime):
            def resource():
                return Response(files('media_tag.web').joinpath(name).read_bytes(), media_type=mime)
            return resource
        app.add_api_route(url, make_resource(name, mime), methods=['GET'], include_in_schema=False)

    def who(principal):
        if principal.role not in ('editor','reader','admin'):
            raise HTTPException(403, 'personnel credential required')
        return {'actor_id':principal.actor_id, 'role':principal.role, 'people':people,
            'features':{'upload':uploads is not None, 'redaction':redactions is not None, 'retime':retiming is not None,'review':reviews is not None,'search':search is not None},
            'terminology_capabilities':{'manage':bool(search and principal.actor_id in search.terminology.config.managers and principal.role!='reader'),
                'publish':bool(search and principal.actor_id in search.terminology.config.publishers and principal.role!='reader')},
            'reviewer':{'redaction':bool(principal.role!='reader' and redactions and principal.actor_id in redactions.reviewers),
                        'retime':bool(principal.role!='reader' and retiming and principal.actor_id in retiming.reviewers)},
            'review_people':{kind:sorted(service.reviewers & editors) if service else [] for kind,service in
                             [('redaction',redactions),('retime',retiming)]},
            'medical_review':'available' if reviews and principal.role!='reader' and principal.actor_id in reviews.config.reviewers else 'not_assigned', 'search':'literal_name_or_id'}

    @app.post('/ui/session')
    def login(request: Request, principal=Depends(identity)):
        return session_response(request,principal)

    def session_response(request,principal):
        from fastapi.responses import JSONResponse
        result = who(principal)
        sessions.remove(request)
        token = sessions.create(principal)
        response = JSONResponse(result)
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=request.url.scheme == 'https', samesite='strict', path='/')
        return response

    @app.get('/ui/login-options')
    def login_options():
        return {'local_browser_login':local_tickets is not None}

    @app.post('/ui/local-session')
    async def local_session(request: Request):
        if local_tickets is None or not local_tickets.allows(request):raise HTTPException(403,'local browser entry required')
        submitted=await body(request,TicketInput)
        try:principal=local_tickets.consume(submitted.ticket,credentials)
        except Exception:raise HTTPException(401,'local browser login expired or unavailable') from None
        return session_response(request,principal)

    @app.get('/ui/session')
    def current(principal=Depends(identity)):
        return who(principal)

    @app.delete('/ui/session')
    def logout(request: Request):
        sessions.remove(request)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path='/')
        return response

    @app.get('/ui/library')
    def listing(q: str='', mine: bool=False, workspace: bool=False, after: str='', limit: int=30, principal=Depends(identity)):
        if len(q)>200 or len(after)>128 or not 1<=limit<=50:
            raise HTTPException(422, 'query limit exceeded')
        return library.assets(principal=principal, query=q, mine=mine, workspace=workspace, after=after, limit=limit)

    @app.get('/ui/library/{asset_id}')
    def membership(asset_id: Identifier, principal=Depends(identity)):
        return library.entry(asset_id, principal=principal)

    @app.post('/ui/library/{asset_id}')
    async def enroll(asset_id: Identifier, request: Request, principal=Depends(identity)):
        data = await body(request, Enrollment)
        return await run_in_threadpool(library.enroll, asset_id, data, principal=principal)

    @app.get('/ui/tasks')
    def tasks(principal=Depends(identity)):
        return library.tasks(principal=principal)

    @app.get('/ui/review-queue')
    def review_queue(principal=Depends(identity)):
        _human(principal)
        from media_tag.transforms.policy import review_state
        with router.database.transaction() as connection:
            rows=connection.execute('''SELECT a.*,v.privacy_status,v.output_sha256,v.recipe_hash,v.asset_id AS variant_id,
                p.spec_json FROM assets a JOIN variants v USING(asset_id) JOIN variant_plans p USING(plan_id)
                WHERE a.deleted_at IS NULL AND v.privacy_status='pending' AND
                (a.owner_id=? OR EXISTS(SELECT 1 FROM asset_permissions WHERE asset_id=a.asset_id AND subject_id=? AND permission IN ('read','edit')))
                ORDER BY a.created_at DESC,a.asset_id LIMIT 100''',(principal.actor_id,principal.actor_id)).fetchall()
            groups={}
            for row in rows:
                kind='redaction' if json.loads(row['spec_json'])['spec']['recipe']['mask'] else 'retime'
                service=redactions if kind=='redaction' else retiming
                if not service or principal.actor_id not in service.reviewers or review_state(connection,row)=='approved':
                    continue
                try:
                    service._review_access(connection,row,principal)
                except AccessDenied:
                    continue
                groups.setdefault(row['root_asset_id'],[]).append(dict(asset_id=row['asset_id'],name=row['name'],sha256=row['blob_sha256'],
                    kind=row['kind'],revision=row['revision'],mine=row['owner_id']==principal.actor_id,
                    file_access=library.files._gate(connection,row) or 'authorized',analysis='waiting_review',
                    technical=library.files._technical(connection,row),index_status='ai_draft'))
        return {'items':[{'root_asset_id':key,'versions':value} for key,value in groups.items()], 'next':None, 'limit':100}

    @app.post('/ui/outputs/{asset_id}/assign-reviewer')
    async def assign_reviewer(asset_id:Identifier, kind:Literal['redaction','retime'], request:Request, principal=Depends(identity)):
        _human(principal)
        data=await body(request,ReviewAssignment)
        service=redactions if kind=='redaction' else retiming
        human_editors={p.actor_id for p in credentials.values() if p.role in ('editor','admin')}
        if not service or data.subject_id not in service.reviewers or data.subject_id not in human_editors:
            raise HTTPException(422,'reviewer must have an explicit configured human binding')
        opened=await run_in_threadpool(service.review_files().open,asset_id,data.output_sha256,principal=principal)
        opened.close()
        with router.database.transaction(write=True) as connection:
            asset=router.catalog._authorize(connection,asset_id,principal)
            if asset['owner_id']!=principal.actor_id:raise AccessDenied('only the owner can assign output review access')
            revision=router.catalog._expected(asset,data.expected_revision)
            service._review_access(connection,asset,principal)
            if service.review_files()._gate(connection,asset,review_asset_id=asset_id):
                raise RevisionConflict('producer or source lineage no longer available')
            connection.execute("INSERT OR IGNORE INTO asset_permissions VALUES(?,?,'read')",(asset_id,data.subject_id))
            connection.execute('UPDATE assets SET revision=? WHERE asset_id=?',(revision,asset_id))
            router.catalog._event(connection,asset_id,revision,principal,'assign_output_reviewer',{},
                {'subject_id':data.subject_id,'output_sha256':data.output_sha256,'kind':kind,'privacy_approved':False})
        return {'asset_id':asset_id,'revision':revision,'subject_id':data.subject_id,'privacy_approved':False}

    @app.post('/ui/tasks/{job_id}/retry')
    async def retry(job_id: Identifier, request: Request, principal=Depends(identity)):
        data = await body(request, Retry)
        return await run_in_threadpool(library.retry, job_id, data.expected_revision, principal=principal)

    @app.api_route('/ui/media/{asset_id}', methods=['GET','HEAD'])
    async def media(asset_id: Identifier, sha256: Sha256, request: Request, download: bool=False,
                    review: Literal['redaction','retime'] | None=None, principal=Depends(identity)):
        service = library.files
        if review:
            if download: raise HTTPException(409, 'review preview is not an approved download')
            transform = redactions if review == 'redaction' else retiming
            if transform is None: raise HTTPException(404, 'review service unavailable')
            service = transform.review_files()
        opened = await run_in_threadpool(service.open, asset_id, sha256, principal=principal)
        return transfer(opened, request, inline=not download)

    @app.api_route('/ui/thumbnail/{asset_id}', methods=['GET','HEAD'])
    async def thumbnail(asset_id: Identifier, sha256: Sha256, request: Request, principal=Depends(identity)):
        # Only evidence from this exact output is eligible, never a parent/unredacted fallback.
        library.files.authorize(asset_id, sha256, principal=principal, for_bytes=True)
        with router.database.transaction() as connection:
            records, _ = library.files._previews(connection, asset_id, sha256)
        if not records: raise FileError(404, 'thumbnail_not_ready')
        first = records[0]
        opened = await run_in_threadpool(library.files.open_preview, asset_id, sha256, first['preview_id'], first['artifact'].sha256, principal=principal)
        return transfer(opened, request, inline=True)

    if redactions:
        @app.get('/ui/scans/{job_id}/frames/{index}')
        def scan_frame(job_id: Identifier, index: int, principal=Depends(identity)):
            status = redactions.get_scan(job_id, principal=principal)
            if not status['data'] or not 0<=index<len(status['data']['frames']):
                raise HTTPException(404, 'scan frame unavailable')
            spec = RevisionSubmission(scan_task_id=status['scan_task_id'], lease_epoch=status['lease_epoch'], report_sha256=status['report_sha256'])
            with router.database.transaction() as connection:
                task, report = redactions._scan(connection, spec, principal)
                row = connection.execute('SELECT result_json FROM router_attempts WHERE task_id=? AND lease_epoch=?', (task.task_id, task.lease_epoch)).fetchone()
                refs = json.loads(row[0])['artifacts']
                ref = next(r['ref'] for r in refs if r['kind']=='face_tracks')
            from pathlib import PurePosixPath
            path = str(PurePosixPath(ref).parent / 'frames' / f'frame-{index+1:06d}.png')
            with router.files.open_read(path) as stream:
                raw = stream.read(64*1024**2+1)
            if len(raw)>64*1024**2 or sha256_bytes(raw)!=report['frames'][index]['decoded_sha256']:
                raise IntegrityError('scan frame changed')
            return Response(raw, media_type='image/png')


def sha256_bytes(raw):
    return sha256(raw).hexdigest()
