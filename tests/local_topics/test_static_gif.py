from datetime import datetime,timezone
from hashlib import sha256
import json

import pytest
from PIL import Image,ImageDraw

from .test_router import topic_router,sought,env
from upload.conftest import tools

from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.service import AnalysisService
from media_tag.catalog import AccessDenied
from media_tag.contracts.models import EffectiveValue,FieldValue
from media_tag.processing_queue import CleaningSubmission,ProcessingDeployment,ProcessingHandlers,ProcessingService,stages
from media_tag.processing_queue.derived import ProcessingDerivedService
from media_tag.processing_queue.derived_contracts import OutputReview,Reviewers
from media_tag.cleaning import CleaningRecipe
from media_tag.router.runtime import CPUHandlers,LocalExecutor,cpu_stages
from media_tag.similarity.pipeline import Handlers,stage
from media_tag.similarity.query import DescriptorQuery
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.upload.models import UploadSpec
from media_tag.upload.service import UploadService


@pytest.mark.parametrize('entry',['registered','upload','derived'])
@pytest.mark.parametrize('frames',[1,2])
def test_actual_gif_modality_reaches_static_ten_fields_only_for_one_frame(topic_router,tools,tmp_path,entry,frames):
    t,e=topic_router,topic_router.env
    e.router.cancel(t.job_id,principal=e.owner)
    root=tmp_path/'gif-source';root.mkdir();(root/'README.md').write_text('Isolated synthetic GIF source; never production approval.\n')
    images=[]
    for color in ('red','blue')[:frames]:
        image=Image.new('RGBA',(32,32),(0,0,0,0));ImageDraw.Draw(image).ellipse((4,4,27,27),fill=color);images.append(image)
    path=root/'unrelated-sticker.gif'
    images[0].save(path,save_all=True,append_images=images[1:],duration=[100]*frames,loop=0,disposal=2,transparency=0)
    original=path.read_bytes();digest=sha256(original).hexdigest()
    if entry=='upload':
        uploads=UploadService(e.router,tools)
        upload=uploads.create(UploadSpec(request_id='gif-upload',filename=path.name,size_bytes=len(original),sha256=digest),principal=e.owner)
        uploads.append(upload['upload_id'],0,original,digest,principal=e.owner)
        done=uploads.complete(upload['upload_id'],principal=e.owner)
        asset=e.catalog.get_asset(done['asset_id'],principal=e.owner)
    else:
        asset=e.catalog.register_file(FilesystemBackend(root),path.name,source_backend_id='synthetic-gif',principal=e.owner,request_id='gif-source',name='Unrelated GIF original')
    if entry=='derived':
        e.router.stages.update(stages(tools));e.router.register_worker(e.worker.actor_id,tuple(stages(tools)),principal=e.admin)
        ids=(e.owner.actor_id,);options=ProcessingDeployment(enabled=True,reviewers=Reviewers(quality=ids,privacy=ids))
        processing=ProcessingService(e.router,tools,options=options)
        kind='image' if frames==1 else 'animation'
        accepted=processing.request(CleaningSubmission(request_id='gif-copy',asset_id=asset.asset_id,source_sha256=asset.sha256,
            expected_revision=asset.revision,recipe=CleaningRecipe(source_sha256=asset.sha256,content_kind=kind)),principal=e.owner)
        assert LocalExecutor(e.router,e.worker,ProcessingHandlers(e.router,e.worker,tools,options=options)).run_once()['accepted']
        derived=ProcessingDerivedService(e.router,tools,options=options)
        status=derived.adopt(accepted['job_id'],principal=e.owner)
        from media_tag.files.service import FileService
        opened=derived.review_files().open(status['asset_id'],status['output_sha256'],principal=e.owner)
        try:
            result_bytes=opened.read(opened.size)
            assert sha256(result_bytes).hexdigest()==digest
            from io import BytesIO
            with Image.open(BytesIO(result_bytes)) as decoded:
                assert decoded.n_frames==frames and decoded.convert('RGBA').getpixel((0,0))[3]==0
        finally:opened.close()
        for dimension in ('quality','privacy'):
            status=derived.review(status['asset_id'],OutputReview(expected_revision=status['revision'],output_sha256=status['output_sha256'],
                recipe_hash=status['recipe_hash'],dimension=dimension,decision='approved',full_output_viewed=True,source_compared=True,
                no_unintended_changes=True,face_coverage_checked=True,note='Synthetic protocol only, not business acceptance'),principal=e.owner)
        asset=e.catalog.get_asset(status['asset_id'],principal=e.owner)
        assert asset.sha256==digest and asset.parent_asset_id is not None
    # Explicit synthetic nonmedical human unknown must survive the AI draft.
    human=EffectiveValue(field='画面主体',value=FieldValue(state='unknown',reason='Synthetic human value retained'),
        actor_id=e.owner.actor_id,revision=asset.revision+1,evidence_ids=('synthetic-gif-source-proof',),confirmed_at=datetime.now(timezone.utc))
    asset=e.catalog.revise(asset.asset_id,principal=e.owner,expected_revision=asset.revision,reason='Synthetic explicit human text value',values=(human,))
    before=e.catalog.get_asset(asset.asset_id,principal=e.owner)
    e.router.stages.update(cpu_stages(tools));e.router.stages['similarity']=stage(tools)
    e.router.register_worker(e.worker.actor_id,('probe','evidence','similarity','local_topics'),principal=e.admin)
    analysis=AnalysisService(e.router,AnalysisDeployment(enabled=True))
    cpu,similarity=CPUHandlers(e.router,e.worker,tools),Handlers(e.router,e.worker,tools)
    executor=LocalExecutor(e.router,e.worker,lambda lease,stopped:t.handler(lease,stopped) if lease.task.stage=='local_topics'
        else similarity(lease,stopped) if lease.task.stage=='similarity' else cpu(lease,stopped))
    for _ in range(24):
        analysis.dispatch_pending(principal=e.owner)
        if executor.run_once() is None:break
    state=analysis.status(asset.asset_id,principal=e.owner)
    assert state['content_kind']==('image' if frames==1 else 'animation'),state
    if frames==1:
        assert state['topic_analysis']['local_topic_stages']['local_topics']=='verified_local_topic_draft'
        assert len(state['topic_analysis']['fields'])==10
        fields={item['field']:item for item in state['topic_analysis']['fields']}
        assert fields['过程/状态']['value']['state']=='unknown'
    else:assert not state['topic_analysis']['local_topic_stages']
    assert not state['analysis_complete'] and not state['medical_confirmation']
    descriptor,result,_=DescriptorQuery(e.router).load(asset.asset_id,principal=e.owner)
    assert result=='ready' and descriptor.media_kind==('image' if frames==1 else 'animation')
    with e.database.transaction() as connection:
        row=connection.execute('''SELECT a.result_json FROM router_tasks t JOIN router_jobs j USING(job_id)
            JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE j.asset_id=? AND t.active=1 AND t.state='succeeded' AND json_extract(t.spec_json,'$.stage')='probe'
            ORDER BY t.rowid DESC LIMIT 1''',(asset.asset_id,)).fetchone()
    ref=next(item for item in json.loads(row[0])['artifacts'] if item['kind']=='technical_report')
    with e.router.files.open_read(ref['ref']) as stream:info=json.load(stream)
    assert info['kind']=='gif' and info['raw_metadata']['image_metadata']['format']=='GIF'
    assert info['raw_metadata']['image_metadata']['frame_count']==frames
    after=e.catalog.get_asset(asset.asset_id,principal=e.owner)
    assert after.owner_id==before.owner_id and after.sha256==digest and after.effective_values==before.effective_values
    assert path.read_bytes()==original
    with pytest.raises(AccessDenied):analysis.status(asset.asset_id,principal=e.other)
