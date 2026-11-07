from hashlib import sha256
import json
import pytest
from router.test_api import TestClient
from media_tag.api.app import create_app
from media_tag.catalog import Principal,AccessDenied,RevisionConflict
from media_tag.files.service import FileError,FileService
from media_tag.transforms.models import RenderSubmission,ReviewSubmission,TrackEdit,Keyframe


def rendering(env, *, request='synthetic-render',mode='solid',revision=None):
    result=env.redactions.start_render(RenderSubmission(request_id=request,revision_hash=(revision or env.revision)['revision_hash'],mode=mode),principal=env.owner)
    assert env.executor.run_once()['accepted']
    run=env.redactions.get_run(result['run_id'],principal=env.owner)
    assert run['job']['state']=='succeeded',run
    assert run['asset_id']
    return run,env.catalog.get_asset(run['asset_id'],principal=env.owner)


def review_spec(env,run,asset,**updates):
    plan=env.redactions.variants.get_plan(run['plan_id'],principal=env.owner)
    return ReviewSubmission.model_validate_json(json.dumps(dict(expected_revision=asset.revision,output_sha256=asset.sha256,
        recipe_hash=plan['recipe_hash'],decision='approved',full_output_viewed=True,exclusions_checked=True,
        risk_intervals_checked=True,note='Synthetic test assertion, not a business review')|updates))


def test_actual_full_video_solid_render_review_download_revoke_and_new_revision(scanned):
    env=scanned; before=env.parent_path.read_bytes(); report=env.scan_report
    assert report['frame_count']==6 and report['all_decoded_frames_observed']
    assert all(len(f['detections'])>=2 for f in report['frames'])
    run,asset=rendering(env); files=FileService(env.router)
    with pytest.raises(FileError,match='privacy_review_pending'): files.open(asset.asset_id,asset.sha256,principal=env.owner)
    private=env.redactions.review_files().open(asset.asset_id,asset.sha256,principal=env.owner)
    assert sha256(private.read(private.size)).hexdigest()==asset.sha256; private.close()
    reviewer=env.reviewer
    with pytest.raises(FileError): env.redactions.review_files().open(asset.asset_id,asset.sha256,principal=reviewer)
    asset=env.catalog.change_access(asset.asset_id,principal=env.owner,expected_revision=1,subject_id=reviewer.actor_id,permission='read',reason='Synthetic review access')
    spec=review_spec(env,run,asset)
    with pytest.raises(AccessDenied): env.redactions.review(asset.asset_id,spec,principal=env.owner)
    with pytest.raises(ValueError): env.redactions.review(asset.asset_id,spec.model_copy(update={'full_output_viewed':False}),principal=reviewer)
    with pytest.raises(RevisionConflict): env.redactions.review(asset.asset_id,spec.model_copy(update={'recipe_hash':'0'*64}),principal=reviewer)
    reviewed=env.redactions.review(asset.asset_id,spec,principal=reviewer)
    opened=files.open(asset.asset_id,asset.sha256,principal=reviewer)
    assert sha256(opened.read(opened.size)).hexdigest()==asset.sha256
    env.redactions.review(asset.asset_id,review_spec(env,run,asset,expected_revision=reviewed['revision'],decision='revoked'),principal=reviewer)
    with pytest.raises(FileError,match='privacy_review_revoked'): opened.read(1)
    opened.close()
    first=next(o['track_id'] for f in report['frames'] for o in f['observations'])
    duration=report['source_clock']['duration_seconds']
    edits=(TrackEdit(action='exclude',track_ids=(first,)),TrackEdit(action='manual',new_track_id='synthetic-manual',
        start_seconds=0.,end_seconds=duration,keyframes=(Keyframe(seconds=0.,box=(.01,.01,.1,.1)),Keyframe(seconds=duration,box=(.06,.01,.15,.1)))))
    revised=env.redactions.create_revision(env.scan_spec.model_copy(update={'edits':edits}),principal=env.owner)
    second,new_asset=rendering(env,request='changed-mask',revision=revised)
    assert new_asset.asset_id!=asset.asset_id and new_asset.sha256!=asset.sha256
    with pytest.raises(FileError,match='privacy_review_pending'): files.open(new_asset.asset_id,new_asset.sha256,principal=env.owner)
    private=env.redactions.review_files().open(new_asset.asset_id,new_asset.sha256,principal=env.owner)
    try:
        image_path=env.workspace/'actual-manual-mask.png'
        env.redactions.variants.tools.ffmpeg_run(['-v','error','-i',str(private.stream.name),'-frames:v','1',str(image_path)],timeout=10)
    finally: private.close()
    from PIL import Image
    with Image.open(image_path) as image: assert max(image.convert('RGB').getpixel((20,20)))<10
    assert env.parent_path.read_bytes()==before and env.catalog.get_asset(env.parent.asset_id,principal=env.owner)==env.parent


def test_actual_mosaic_http_permissions_ranges_and_preview_are_output_only(scanned):
    env=scanned; run,asset=rendering(env,mode='mosaic')
    token={who:sha256(who.encode()).hexdigest() for who in ('owner','worker','other','reviewer')}
    identities={token['owner']:env.owner,token['worker']:env.workers[0],token['other']:Principal(actor_id='synthetic-other'),token['reviewer']:env.reviewer}
    app=create_app(env.router,identities,redactions=env.redactions); auth=lambda who:{'Authorization':'Bearer '+who}
    with TestClient(app) as client:
        with env.database.transaction() as connection:
            scan_job=connection.execute('SELECT job_id FROM router_tasks WHERE task_id=?',(env.scan_spec.scan_task_id,)).fetchone()[0]
        scan_response=client.get('/redaction/scans/'+scan_job,headers=auth('owner'))
        assert scan_response.status_code==200 and scan_response.json()['data']==env.scan_report
        assert client.get('/redaction/scans/'+scan_job,headers=auth('other')).status_code==403
        returned=scan_response.json()
        payload={key:returned[key] for key in ('scan_task_id','lease_epoch','report_sha256')}
        assert client.post('/redaction/revisions',headers=auth('owner'),json=payload).json()['revision_hash']==env.revision['revision_hash']
        url=f'/redaction/outputs/{asset.asset_id}/preview-content?sha256={asset.sha256}'
        assert client.get(url).status_code==401 and client.get(url,headers=auth('worker')).status_code==403 and client.get(url,headers=auth('other')).status_code==403
        response=client.get(url,headers=auth('owner')|{'Range':'bytes=0-31'})
        assert response.status_code==206 and len(response.content)==32 and response.headers['cache-control']=='private, no-store'
        assert client.get(f'/v1/assets/{asset.asset_id}/content?sha256={asset.sha256}',headers=auth('owner')).status_code==409
        assert client.post('/redaction/outputs/'+asset.asset_id+'/review',headers=auth('worker'),json=review_spec(env,run,asset).model_dump(mode='json')).status_code==403
        assert client.post('/redaction/renders',headers=auth('owner'),json={'request_id':'bad','revision_hash':env.revision['revision_hash'],'speed':2}).status_code==422
        asset=env.catalog.change_access(asset.asset_id,principal=env.owner,expected_revision=1,subject_id=env.reviewer.actor_id,permission='read',reason='Synthetic reviewer read grant')
        response=client.post('/redaction/outputs/'+asset.asset_id+'/review',headers=auth('reviewer'),json=review_spec(env,run,asset).model_dump(mode='json'))
        assert response.status_code==200
        public=f'/v1/assets/{asset.asset_id}/content?sha256={asset.sha256}'
        response=client.get(public,headers=auth('owner'))
        assert response.status_code==200 and sha256(response.content).hexdigest()==asset.sha256
        previews=client.get(f'/v1/assets/{asset.asset_id}/preview-manifest?sha256={asset.sha256}',headers=auth('owner')).json()
        assert previews['resources'] and all(p['sha256']==asset.sha256 for p in previews['resources'])
        with env.database.transaction() as connection:
            key=connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=?',(asset.sha256,)).fetchone()[0]
        path=env.objects/key; original=path.read_bytes()
        try:
            path.write_bytes(b'synthetic broken output')
            response=client.get(public,headers=auth('owner'))
            assert response.status_code==503 and env.parent.sha256 not in response.text
            current=env.catalog.get_asset(asset.asset_id,principal=env.owner)
            revoked=review_spec(env,run,current,decision='revoked',full_output_viewed=False,exclusions_checked=False,risk_intervals_checked=False)
            assert client.post('/redaction/outputs/'+asset.asset_id+'/review',headers=auth('reviewer'),json=revoked.model_dump(mode='json')).status_code==200
            assert client.get(public,headers=auth('owner')).status_code==409
        finally: path.write_bytes(original)


def test_scan_cannot_be_adopted_from_stale_lease(scanned):
    env=scanned
    with pytest.raises(AccessDenied): env.redactions.create_revision(env.scan_spec,principal=env.workers[0])
    with env.database.transaction() as connection:
        job_id=connection.execute('SELECT job_id FROM router_tasks WHERE task_id=?',(env.scan_spec.scan_task_id,)).fetchone()[0]
    env.router.cancel(job_id,principal=env.owner)
    with pytest.raises(RevisionConflict): env.redactions.revision(env.revision['revision_hash'],principal=env.owner)
