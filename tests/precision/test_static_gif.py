from hashlib import sha256
from io import BytesIO
import json

from PIL import Image,ImageDraw

from .conftest import precise,sought,env,tools
from media_tag.contracts.models import WorkerResult
from media_tag.precision.projection import verified


def test_actual_single_gif_precision_uses_image_evidence_and_current_projection(precise):
    p=precise
    image=Image.new('RGB',(24,24),'white');ImageDraw.Draw(image).ellipse((3,3,20,20),fill='red')
    stream=BytesIO();image.save(stream,format='GIF',loop=0,duration=100);data=stream.getvalue()
    asset=p.register(data,'static-sticker.gif','image')
    deployment=p.deployment()
    spec,accepted,executor=p.submit(deployment,asset_id=asset.asset_id,request='static-gif-precision')
    assert executor.run_once()['accepted']
    state=p.analysis.status(asset.asset_id,principal=p.env.owner)
    assert state['content_kind']=='image' and 'verified_precision_draft' in state['topic_analysis']['precision_stages'].values()
    with p.env.database.transaction() as connection:
        row=connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at FROM router_tasks t
            JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE t.job_id=? AND t.active=1 AND json_extract(t.spec_json,'$.stage')='precision' ''',(accepted['job_id'],)).fetchone()
        source=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset.asset_id,)).fetchone()
    task,result,report,ref=verified(p.env.router,row,source,'image')
    sample=report.document.evidence[0]
    assert sample.kind=='image' and sample.start_seconds is None and sample.end_seconds is None
    with p.env.router.files.open_read(sample.artifact.ref) as original:
        assert original.read()==data
    assert report.document.content_sha256==sha256(data).hexdigest() and len(report.document.fields)==10
    assert not report.document.medical_confirmation and not report.document.privacy_confirmation
