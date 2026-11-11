from hashlib import sha256
import json

import pytest
from PIL import Image

from media_tag.catalog import AccessDenied,Principal,RevisionConflict
from media_tag.cleaning import CleaningRecipe
from media_tag.cleaning.models import VideoSegment
from media_tag.files.service import FileService,FileError
from media_tag.image_processing import ImageRecipe,MaskRegion
from media_tag.processing_queue import CleaningSubmission,ImageSubmission
from media_tag.processing_queue.derived import ProcessingDerivedService
from media_tag.processing_queue.derived_contracts import OutputReview,Reviewers
from media_tag.workers.probe import file_sha256

from .conftest import register
from .test_queue import submission,run


def service(queue,reviewers=True):
    ids=(queue.env.owner.actor_id,) if reviewers else ()
    options=queue.options.model_copy(update={'reviewers':Reviewers(quality=ids,watermark=ids,privacy=ids),'auto_adopt':True})
    return ProcessingDerivedService(queue.env.router,queue.tools,options=options)


def approve(derived,status,dimension,principal):
    spec=OutputReview(expected_revision=status['revision'],output_sha256=status['output_sha256'],recipe_hash=status['recipe_hash'],
        dimension=dimension,decision='approved',full_output_viewed=True,source_compared=True,no_unintended_changes=True,
        watermark_regions_checked=True,face_coverage_checked=True)
    return derived.review(status['asset_id'],spec,principal=principal)


def produced(queue,recipe=None):
    asset=register(queue)
    if recipe is None:recipe=CleaningRecipe(source_sha256=asset.sha256,content_kind='image')
    job,status=run(queue,submission(queue,asset,recipe,ImageSubmission if isinstance(recipe,ImageRecipe) else CleaningSubmission))
    assert status['processing_completed'],status
    return asset,job


def test_real_standard_copy_registers_distinct_derived_and_independent_reviews(queue):
    asset,job=produced(queue);derived=service(queue)
    status=derived.adopt(job,principal=queue.env.owner)
    assert status['asset_id']!=asset.asset_id and status['output_sha256']==asset.sha256
    assert status['reviews']=={'quality':'pending','watermark':'not_applicable','privacy':'pending'}
    assert derived.adopt(job,principal=queue.env.owner)['asset_id']==status['asset_id']
    with queue.env.database.transaction() as connection:
        row=connection.execute('SELECT * FROM assets WHERE asset_id=?',(status['asset_id'],)).fetchone()
        assert row['kind']=='derived' and row['parent_asset_id']==asset.asset_id
        assert connection.execute('SELECT count(*) FROM variant_source_links WHERE asset_id=?',(status['asset_id'],)).fetchone()[0]==1
        assert connection.execute('SELECT count(*) FROM effective_values WHERE asset_id=?',(status['asset_id'],)).fetchone()[0]==0
        assert connection.execute('SELECT content_kind FROM analysis_intents WHERE asset_id=?',(status['asset_id'],)).fetchone()[0]=='image'
    ordinary=FileService(queue.env.router)
    with pytest.raises(FileError):ordinary.open(status['asset_id'],status['output_sha256'],principal=queue.env.owner)
    comparison=derived.comparison(status['asset_id'],principal=queue.env.owner)
    assert comparison['source_sha256']==asset.sha256
    opened=derived.review_files().open(status['asset_id'],status['output_sha256'],principal=queue.env.owner)
    assert sha256(opened.read(opened.size)).hexdigest()==asset.sha256;opened.close()
    status=approve(derived,status,'privacy',queue.env.owner)
    assert status['reviews']['quality']=='pending' and not status['output_download_available']
    status=approve(derived,status,'quality',queue.env.owner)
    assert status['output_download_available'] and status['state']=='available'
    manifest=ordinary.download_manifest(status['asset_id'],status['output_sha256'],principal=queue.env.owner)
    assert manifest['transform']['schema_version']=='ct-processing-variant-v1'
    assert manifest['transform']['parent_to_output_time_map']['state']=='not_applicable'
    assert not status['license_confirmation'] and not status['medical_confirmation']
    spec=OutputReview(expected_revision=status['revision'],output_sha256=status['output_sha256'],recipe_hash=status['recipe_hash'],dimension='quality',decision='revoked')
    revoked=derived.review(status['asset_id'],spec,principal=queue.env.owner)
    assert not revoked['output_download_available']
    with pytest.raises(FileError):ordinary.open(status['asset_id'],status['output_sha256'],principal=queue.env.owner)
    assert file_sha256(queue.env.originals/'image.png')==asset.sha256


def test_actual_apng_mask_registration_and_watermark_gate(queue):
    path=queue.env.originals/'sticker.gif'
    Image.new('RGBA',(24,16),'red').save(path,save_all=True,append_images=[Image.new('RGBA',(24,16),'blue')],duration=[100,200],loop=0)
    asset=register(queue,path.name)
    recipe=ImageRecipe(output_format='apng',regions=(MaskRegion(region_id='watermark',purpose='watermark',box=(.2,.2,.8,.8),method='solid'),))
    job,status=run(queue,submission(queue,asset,recipe,ImageSubmission))
    derived=service(queue);status=derived.adopt(job,principal=queue.env.owner)
    assert status['content_kind']=='animation' and status['reviews']['watermark']=='pending'
    with queue.env.database.transaction() as connection:
        validation=json.loads(connection.execute('SELECT validation_json FROM variants WHERE asset_id=?',(status['asset_id'],)).fetchone()[0])
    assert validation['validation_method']=='pillow_full_frames' and validation['frame_count']==2
    assert validation['frame_delays']==[{'numerator':1,'denominator':10},{'numerator':1,'denominator':5}]
    status=approve(derived,status,'quality',queue.env.owner);status=approve(derived,status,'privacy',queue.env.owner)
    assert not status['output_download_available']
    status=approve(derived,status,'watermark',queue.env.owner)
    assert status['output_download_available']


def test_actual_video_crop_map_is_registered_without_old_fixed_map(queue):
    path=queue.env.originals/'derived.mp4'
    queue.tools.ffmpeg_run(['-f','lavfi','-i','testsrc2=size=64x48:rate=10:duration=6','-c:v','libx264','-threads','1',str(path)],timeout=20)
    asset=register(queue,path.name)
    recipe=CleaningRecipe(source_sha256=asset.sha256,content_kind='video',segment=VideoSegment(start_seconds=1,end_seconds=5),preset='ultrafast')
    job,_=run(queue,submission(queue,asset,recipe));derived=service(queue)
    status=derived.adopt(job,principal=queue.env.owner)
    assert status['time_map']['state']=='available' and status['time_map']['mapping']['speed']==1
    assert status['time_map']['mapping']['output_duration_seconds']==pytest.approx(4)
    with queue.env.database.transaction() as connection:
        raw=json.loads(connection.execute('SELECT p.spec_json FROM variants v JOIN variant_plans p USING(plan_id) WHERE v.asset_id=?',(status['asset_id'],)).fetchone()[0])
    assert raw['schema_version']=='ct-processing-variant-v1' and raw['recipe']['segment']['start_seconds']==1
    assert 'clock' not in raw['time_map']


def test_audio_face_privacy_not_applicable_is_not_voice_or_license_approval(queue):
    path=queue.env.originals/'bgm.wav'
    queue.tools.ffmpeg_run(['-f','lavfi','-i','sine=frequency=440:duration=4',str(path)],timeout=20)
    asset=register(queue,path.name)
    job,_=run(queue,submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='audio')))
    derived=service(queue);status=derived.adopt(job,principal=queue.env.owner)
    assert status['reviews']['privacy']=='not_applicable' and status['time_map']['reason']=='audio_not_retimed'
    assert not status['audio_voice_privacy_reviewed'] and not status['license_confirmation']
    status=approve(derived,status,'quality',queue.env.owner)
    assert status['output_download_available']


def test_default_no_reviewers_and_no_automatic_actor_impersonation(queue):
    asset,job=produced(queue);derived=service(queue,reviewers=False)
    assert derived.adopt_ready()==[]
    outcomes=derived.adopt_ready(principals=(queue.env.owner,))
    status=derived.status(outcomes[0]['asset_id'],principal=queue.env.owner)
    with pytest.raises(AccessDenied):approve(derived,status,'quality',queue.env.owner)
    disabled=ProcessingDerivedService(queue.env.router,queue.tools)
    with pytest.raises(RevisionConflict):disabled.review_files().open(status['asset_id'],status['output_sha256'],principal=queue.env.owner)


def test_adoption_rechecks_source_during_actual_validation(queue,monkeypatch):
    from media_tag.processing_queue import derived as module
    asset,job=produced(queue);derived=service(queue)
    actual=module.validate_output
    def changed(*args):
        value=actual(*args)
        queue.env.catalog.revise(asset.asset_id,principal=queue.env.owner,expected_revision=asset.revision,reason='during probe',name='changed')
        return value
    monkeypatch.setattr(module,'validate_output',changed)
    with pytest.raises(RevisionConflict):derived.adopt(job,principal=queue.env.owner)
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM processing_derivations').fetchone()[0]==0
        assert connection.execute('SELECT count(*) FROM variant_plans').fetchone()[0]==0


def test_failed_commit_rolls_back_plan_variant_links_and_intent(queue,monkeypatch):
    from media_tag.analysis import intake
    asset,job=produced(queue);derived=service(queue)
    original=intake.ensure_intent
    def failed(*args,**kwargs):raise ValueError('synthetic short transaction failure')
    monkeypatch.setattr(intake,'ensure_intent',failed)
    with pytest.raises(ValueError,match='short transaction'):derived.adopt(job,principal=queue.env.owner)
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM processing_derivations').fetchone()[0]==0
        assert connection.execute('SELECT count(*) FROM variants').fetchone()[0]==0
        assert connection.execute('SELECT count(*) FROM variant_plans').fetchone()[0]==0
    monkeypatch.setattr(intake,'ensure_intent',original)
    assert derived.adopt(job,principal=queue.env.owner)['asset_registered']


def test_designated_reviewer_requires_current_output_and_source_acl(queue):
    asset,job=produced(queue);reviewer=Principal(actor_id='designated-only')
    options=queue.options.model_copy(update={'reviewers':Reviewers(quality=(reviewer.actor_id,))})
    derived=ProcessingDerivedService(queue.env.router,queue.tools,options=options)
    status=derived.adopt(job,principal=queue.env.owner)
    with pytest.raises(AccessDenied):derived.comparison(status['asset_id'],principal=reviewer)
    granted=queue.env.catalog.change_access(status['asset_id'],principal=queue.env.owner,expected_revision=status['revision'],
        subject_id=reviewer.actor_id,permission='edit',reason='explicit output grant')
    with pytest.raises(AccessDenied):derived.comparison(status['asset_id'],principal=reviewer)
    queue.env.catalog.change_access(asset.asset_id,principal=queue.env.owner,expected_revision=asset.revision,
        subject_id=reviewer.actor_id,permission='read',reason='explicit source comparison grant')
    status=derived.status(status['asset_id'],principal=reviewer)
    assert derived.comparison(status['asset_id'],principal=reviewer)['source_sha256']==asset.sha256
    status=approve(derived,status,'quality',reviewer)
    assert status['reviews']['quality']=='approved' and status['reviews']['privacy']=='pending'
    queue.env.catalog.change_access(status['asset_id'],principal=queue.env.owner,expected_revision=status['revision'],
        subject_id=reviewer.actor_id,permission='edit',reason='revoke output review',revoke=True)
    with pytest.raises(AccessDenied):derived.comparison(status['asset_id'],principal=reviewer)


def test_actual_face_processing_is_distinct_from_privacy_and_propagates_only_valid_lineage(queue):
    from media_tag.transforms.policy import has_reviewed_face_processing
    def face_state(asset_id):
        with queue.env.database.transaction() as connection:
            asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
            return has_reviewed_face_processing(connection,asset)
    source,job=produced(queue);derived=service(queue)
    copy=derived.adopt(job,principal=queue.env.owner)
    copy=approve(derived,copy,'privacy',queue.env.owner);copy=approve(derived,copy,'quality',queue.env.owner)
    assert not face_state(copy['asset_id'])
    original=queue.env.catalog.get_asset(source.asset_id,principal=queue.env.owner)
    recipe=ImageRecipe(output_format='png',regions=(MaskRegion(region_id='face',purpose='face',box=(.2,.2,.8,.8),method='solid'),))
    job,_=run(queue,submission(queue,original,recipe,ImageSubmission,request_id='actual-face'))
    face=derived.adopt(job,principal=queue.env.owner)
    assert not face_state(face['asset_id'])
    face=approve(derived,face,'privacy',queue.env.owner);face=approve(derived,face,'quality',queue.env.owner)
    assert face_state(face['asset_id'])
    parent=queue.env.catalog.get_asset(face['asset_id'],principal=queue.env.owner)
    child_job,_=run(queue,submission(queue,parent,CleaningRecipe(source_sha256=parent.sha256,content_kind='image'),request_id='copy-face-parent'))
    child=derived.adopt(child_job,principal=queue.env.owner)
    child=approve(derived,child,'privacy',queue.env.owner);child=approve(derived,child,'quality',queue.env.owner)
    assert face_state(child['asset_id'])
    derived.review(face['asset_id'],OutputReview(expected_revision=face['revision'],output_sha256=face['output_sha256'],recipe_hash=face['recipe_hash'],dimension='privacy',decision='revoked'),principal=queue.env.owner)
    assert not face_state(child['asset_id'])
    with pytest.raises(FileError):FileService(queue.env.router).open(child['asset_id'],child['output_sha256'],principal=queue.env.owner)
    with pytest.raises(FileError):derived.review_files().open(child['asset_id'],child['output_sha256'],principal=queue.env.owner)


def test_processing_v12_backup_restore_preserves_contract_and_independent_reviews(queue,tmp_path):
    from media_tag.maintenance.backup import snapshot,restore
    from media_tag.catalog import Catalog,Database
    from media_tag.storage.filesystem import FilesystemBackend
    from media_tag.router.service import Router
    source,job=produced(queue);derived=service(queue)
    status=derived.adopt(job,principal=queue.env.owner)
    status=approve(derived,status,'privacy',queue.env.owner);status=approve(derived,status,'quality',queue.env.owner)
    backup=snapshot(queue.env.database.path,queue.env.workspace,queue.env.objects,tmp_path/'processing-backup',offline_confirmed=True)
    restore(tmp_path/'processing-backup',tmp_path/'processing-restored',backup['manifest_sha256'])
    root=tmp_path/'processing-restored'
    catalog=Catalog(Database(root/'database.sqlite3'),FilesystemBackend(root/'objects',writable=True),contract=queue.env.contract)
    router=Router(catalog,root/'workspace',queue.env.router.stages)
    restored=ProcessingDerivedService(router,queue.tools,options=derived.options)
    final=restored.status(status['asset_id'],principal=queue.env.owner)
    assert final['reviews']==status['reviews'] and final['output_download_available']
    opened=FileService(router).open(status['asset_id'],status['output_sha256'],principal=queue.env.owner)
    assert sha256(opened.read(opened.size)).hexdigest()==status['output_sha256'];opened.close()


@pytest.mark.parametrize('change',['cancel','epoch'])
def test_adoption_producer_change_during_probe_never_registers(queue,monkeypatch,change):
    from media_tag.processing_queue import derived as module
    asset,job=produced(queue);derived=service(queue)
    actual=module.validate_output
    def changed(*args):
        value=actual(*args)
        if change=='cancel':queue.env.router.cancel(job,principal=queue.env.owner)
        else:
            with queue.env.database.transaction(write=True) as connection:
                connection.execute('UPDATE router_tasks SET lease_epoch=lease_epoch+1 WHERE job_id=?',(job,))
        return value
    monkeypatch.setattr(module,'validate_output',changed)
    with pytest.raises((RevisionConflict,ValueError)):derived.adopt(job,principal=queue.env.owner)
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM processing_derivations').fetchone()[0]==0


def test_reviews_are_immutable_and_bad_output_recipe_or_incomplete_checks_cannot_approve(queue):
    import sqlite3
    asset,job=produced(queue);derived=service(queue)
    status=derived.adopt(job,principal=queue.env.owner)
    spec=OutputReview(expected_revision=status['revision'],output_sha256=status['output_sha256'],recipe_hash=status['recipe_hash'],dimension='quality',decision='approved')
    with pytest.raises(ValueError):derived.review(status['asset_id'],spec,principal=queue.env.owner)
    complete=spec.model_copy(update={'full_output_viewed':True,'source_compared':True,'no_unintended_changes':True,'recipe_hash':'0'*64})
    with pytest.raises(RevisionConflict):derived.review(status['asset_id'],complete,principal=queue.env.owner)
    status=approve(derived,status,'quality',queue.env.owner);status=approve(derived,status,'privacy',queue.env.owner)
    for table in ('processing_output_reviews','privacy_reviews'):
        with pytest.raises(sqlite3.IntegrityError):
            with queue.env.database.transaction(write=True) as connection:
                connection.execute('DELETE FROM '+table+' WHERE asset_id=?',(status['asset_id'],))


def test_eight_failed_adoptions_do_not_starve_later_work_and_restart_retries_idempotently(queue,monkeypatch):
    from media_tag.processing_queue import derived as module
    blocked=set();jobs=[]
    for index in range(9):
        asset=register(queue,f'batch-{index}.png')
        job,state=run(queue,submission(queue,asset,CleaningRecipe(source_sha256=asset.sha256,content_kind='image'),request_id=f'job-{index}'))
        jobs.append(job)
        if index<8:blocked.add(asset.asset_id)
    derived=service(queue);original=module.validate_output
    def fail_first(path,tools,config,report):
        if report.asset_id in blocked:raise ValueError('synthetic temporary validator failure')
        return original(path,tools,config,report)
    monkeypatch.setattr(module,'validate_output',fail_first)
    failures=derived.adopt_ready(principals=(queue.env.owner,))
    assert len(failures)==8 and all(item['state']=='adoption_not_completed' for item in failures)
    ready=derived.adopt_ready(principals=(queue.env.owner,))
    assert len(ready)==1 and ready[0]['job_id']==jobs[-1] and ready[0]['state']=='registered_pending_review'
    failed_status=queue.service.status(jobs[0],principal=queue.env.owner)
    assert not failed_status['asset_registered'] and failed_status['registration_attempt']['code']=='adoption_validation_failed'
    monkeypatch.setattr(module,'validate_output',original)
    restarted=ProcessingDerivedService(queue.env.router,queue.tools,options=derived.options)
    assert restarted.adopt_ready(principals=(queue.env.owner,))==[]
    queue.env.now[0]+=3
    recovered=restarted.adopt_ready(principals=(queue.env.owner,))
    assert len(recovered)==8 and all(item['state']=='registered_pending_review' for item in recovered)
    assert restarted.adopt_ready(principals=(queue.env.owner,))==[]
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM processing_derivations').fetchone()[0]==9
        assert connection.execute("SELECT count(*) FROM processing_adoption_receipts WHERE state='registered'").fetchone()[0]==9


def test_approval_requires_actual_source_bytes_but_revocation_does_not(queue):
    source=register(queue)
    recipe=ImageRecipe(output_format='png',regions=(MaskRegion(region_id='face',purpose='face',box=(.2,.2,.8,.8),method='solid'),))
    job,_=run(queue,submission(queue,source,recipe,ImageSubmission));derived=service(queue)
    status=derived.adopt(job,principal=queue.env.owner)
    with queue.env.database.transaction() as connection:
        key=connection.execute('SELECT object_key FROM storage_locations WHERE blob_sha256=?',(source.sha256,)).fetchone()[0]
    (queue.env.objects/key).write_bytes(b'synthetic corruption')
    with pytest.raises(FileError):approve(derived,status,'quality',queue.env.owner)
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM processing_output_reviews').fetchone()[0]==0
    spec=OutputReview(expected_revision=status['revision'],output_sha256=status['output_sha256'],recipe_hash=status['recipe_hash'],dimension='quality',decision='revoked')
    assert derived.review(status['asset_id'],spec,principal=queue.env.owner)['reviews']['quality']=='revoked'


def test_admin_cannot_adopt_another_actors_job_or_transfer_derived_ownership(queue):
    asset=register(queue)
    updated=queue.env.catalog.change_access(asset.asset_id,principal=queue.env.owner,expected_revision=asset.revision,
        subject_id=queue.env.admin.actor_id,permission='edit',reason='explicit admin object permission, not ownership delegation')
    job,_=run(queue,submission(queue,updated,CleaningRecipe(source_sha256=updated.sha256,content_kind='image')))
    derived=service(queue)
    with pytest.raises(AccessDenied):derived.adopt(job,principal=queue.env.admin)
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT count(*) FROM variants').fetchone()[0]==0
        assert connection.execute('SELECT count(*) FROM processing_adoption_receipts').fetchone()[0]==0
    status=derived.adopt(job,principal=queue.env.owner)
    with queue.env.database.transaction() as connection:
        assert connection.execute('SELECT owner_id FROM assets WHERE asset_id=?',(status['asset_id'],)).fetchone()[0]==queue.env.owner.actor_id
        assert connection.execute('SELECT p.owner_id FROM variant_plans p JOIN variants v USING(plan_id) WHERE v.asset_id=?',(status['asset_id'],)).fetchone()[0]==queue.env.owner.actor_id
