"""Real bounded decoding subprocesses; protocol-only generator, no GPU weights."""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from search.conftest import sought,env
from upload.conftest import tools
from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.contracts.models import TaskLimits,Versions
from media_tag.router.models import JobSpec,StepSpec
from media_tag.router.runtime import CPUHandlers,LocalExecutor,cpu_stages
from media_tag.semantic.registry import SemanticRegistry
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.temporal_topics.contracts import Deployment,Options,TaskOptions,Rational
from media_tag.temporal_topics.native import _processor_identity,input_digest
from media_tag.temporal_topics.process import run_child
from media_tag.temporal_topics.projection import verified,merge
from media_tag.temporal_topics.prompt import PROMPT_VERSION,PREPROCESS_VERSION
from media_tag.temporal_topics.runtime import TemporalTopicHandlers,stage,tool_digest


WIRE='''import json
import os
import sys
import time
from pathlib import Path
from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.semantic.registry import SemanticRegistry
from media_tag.temporal_topics.contracts import Generation
from media_tag.temporal_topics.native import _processor_identity
import media_tag.temporal_topics.runner as runner
MODE = __MODE__
class Protocol:
    def __init__(self,root,enabled=False):self.model_revision=SemanticRegistry(Path(root)).revision
    def __call__(self,**kwargs):
        assert os.environ.get('HF_HUB_OFFLINE')=='1'
        assert 'TEMPORAL_FAKE_SECRET' not in os.environ
        assert len(kwargs['frame_bytes'])>=2
        answers=tuple(json.dumps({'field':field,'state':'observed' if field=='英文检索词' else 'unknown',
            'values':['red then blue'] if field=='英文检索词' else [],'dynamic':field=='英文检索词'}) for field in TOPIC_FIELDS)
        return Generation(backend='synthetic_protocol' if MODE=='synthetic' else 'local_qwen_video',
            model_revision=self.model_revision,prompt_version=runner.PROMPT_VERSION,
            input_sha256=kwargs['input_sha256'],processor_identity=_processor_identity(),answers=answers)
runner.LocalQwenVideoGenerator=Protocol
if MODE=='decode-hang':
    import media_tag.temporal_topics.service as service
    def blocked(*args):
        import subprocess
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        Path(sys.argv[1]).with_suffix('.decoder-started').write_text(json.dumps([os.getpid(),child.pid]))
        time.sleep(60)
    service.animation=blocked
if MODE in ('logs','response'):
    def excessive(request):
        if MODE=='logs':
            sys.stdout.write('x'*(3*1024**2));sys.stdout.flush()
        else:Path(sys.argv[2]).write_bytes(b'x'*(2*1024**2))
        time.sleep(60)
    runner.execute=excessive
if __name__=='__main__':raise SystemExit(runner.main())
'''


@pytest.fixture
def temporal_worker(sought,tools,tmp_path):
    e=sought.env
    with e.database.transaction(write=True) as connection:connection.execute('UPDATE router_jobs SET cancelled=1')
    root=tmp_path/'absent-weights';root.mkdir();(root/'README.md').write_text('No model weights: protocol fixture only.\n')
    registry=SemanticRegistry(root)
    e.router.stages.update(cpu_stages(tools));e.router.stages['temporal_topics']=stage(registry,tools)
    e.router.register_worker(e.worker.actor_id,('probe','temporal_topics'),principal=e.admin)
    inputs=tmp_path/'temporal-inputs';inputs.mkdir();(inputs/'README.md').write_text('Synthetic immutable originals.\n')
    scripts=tmp_path/'protocol';scripts.mkdir();(scripts/'README.md').write_text('Isolated protocol mock, never a model.\n')
    script=scripts/'wire.py';script.write_text(WIRE.replace('__MODE__',repr('normal')),encoding='utf8')
    class Handler(TemporalTopicHandlers):
        def invoke(self,request,output,task,stopped,*,deadline):
            return run_child(request,output,stopped,deadline=deadline,
                command=lambda a,b:[sys.executable,str(script),str(a),str(b)])
    handler=Handler(e.router,e.worker,Deployment(enabled=True,model_root=str(root)),tools)
    cpu=CPUHandlers(e.router,e.worker,tools)
    errors=[];stops=[]
    def dispatch(lease,stop):
        if lease.task.stage=='temporal_topics':stops.append(stop)
        try:return handler(lease,stop) if lease.task.stage=='temporal_topics' else cpu(lease,stop)
        except Exception as exc:
            errors.append(repr(exc));raise
    executor=LocalExecutor(e.router,e.worker,dispatch)
    def submit(data=None,*,name='cycle.gif',kind='animation',timeout=120,options=None):
        if data is None:
            buffer=BytesIO();Image.new('RGB',(24,24),'red').save(buffer,format='GIF',save_all=True,
                append_images=[Image.new('RGB',(24,24),'blue')],duration=[80,120],loop=0);data=buffer.getvalue()
        (inputs/name).write_bytes(data)
        source=e.catalog.register_file(FilesystemBackend(inputs),name,source_backend_id='temporal-synthetic',
            principal=e.owner,request_id='source-'+name,name=name)
        config=TaskOptions(model_revision=registry.revision,policy_digest=sought.reviews.policy.digest,
            tool_digest=tool_digest(tools),content_kind=kind,options=options or Options())
        spec=StepSpec(key='temporal',stage='temporal_topics',depends_on=('probe',),target_fields=TOPIC_FIELDS,
            versions=Versions(rules_version='sha256:'+config.policy_digest,preprocess_version=PREPROCESS_VERSION,
                taxonomy_version=sought.reviews.policy.config.taxonomy_version,model_revision=registry.revision),
            prompt_version=PROMPT_VERSION,config=config.model_dump(mode='json'),limits=TaskLimits(gpu_slots=1,timeout_seconds=timeout))
        probe=StepSpec(key='probe',stage='probe',versions=Versions(rules_version='synthetic-probe',preprocess_version='d02-cpu-v1'))
        job=e.router.submit(JobSpec(asset_id=source.asset_id,steps=(probe,spec)),request_id='job-'+name,principal=e.owner)
        return source,job,spec
    def rows(job_id):
        with e.database.transaction() as connection:
            row=connection.execute('''SELECT t.*,a.envelope_json,a.result_json,a.released_at FROM router_tasks t
                LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
                WHERE t.job_id=? AND t.active=1 AND t.step_key='temporal' ''',(job_id,)).fetchone()
            asset=connection.execute('SELECT * FROM assets WHERE asset_id=(SELECT asset_id FROM router_jobs WHERE job_id=?)',(job_id,)).fetchone()
            job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(job_id,)).fetchone()
        return row,asset,job
    return SimpleNamespace(env=e,sought=sought,tools=tools,handler=handler,executor=executor,submit=submit,rows=rows,
        script=script,inputs=inputs,registry=registry,errors=errors,stops=stops)


def execute(t):
    assert t.executor.run_once()['accepted']
    result=t.executor.run_once();assert result['accepted'],result


def test_router_real_decode_in_child_and_strict_source_revalidation(temporal_worker,monkeypatch):
    import media_tag.temporal_topics.service as service
    t=temporal_worker;source,job,_=t.submit()
    # A parent-side decoder invocation would fail this test; the fresh child owns it.
    monkeypatch.setattr(service,'animation',lambda *args:(_ for _ in ()).throw(AssertionError('decoder in parent')))
    monkeypatch.setenv('TEMPORAL_FAKE_SECRET','do-not-inherit')
    execute(t)
    row,asset,job=t.rows(job['job_id']);assert row['state']=='succeeded',(row['error_code'],t.errors)
    task,result,report,ref=verified(t.env.router,row,asset)
    assert len(report.result.sequence.frames)==2
    assert report.result.sequence.model_frame_sources==(0,0,1,1,1)
    assert all(part.ref.startswith(task.output_prefix) for part in result.artifacts)
    assert next(item for item in report.result.document.fields if item.field=='英文检索词').dynamic
    assert report.result.document.evidence[0].end_seconds==.2
    with t.env.database.transaction() as connection:
        fields,evidence,producers,states=merge(t.env.router,connection,asset,'animation',job,{},(),())
    assert fields['英文检索词'].value.values==('red then blue',)
    assert fields['英文检索词'].dynamic and len(evidence)==1 and len(producers)==1
    assert states['temporal']=='verified_temporal_topic_draft'
    assert t.env.catalog.get_asset(source.asset_id,principal=t.env.owner).effective_values==()


def test_synthetic_backend_cannot_enter_production_result(temporal_worker):
    t=temporal_worker;t.script.write_text(WIRE.replace('__MODE__',repr('synthetic')),encoding='utf8')
    _,job,_=t.submit();execute(t)
    row,_,_=t.rows(job['job_id'])
    assert row['state'] in ('failed','needs_attention') and row['error_code']=='temporal_synthetic_result_rejected'


def test_decoder_is_inside_total_child_deadline_and_quota_is_released(temporal_worker):
    t=temporal_worker;t.script.write_text(WIRE.replace('__MODE__',repr('decode-hang')),encoding='utf8')
    _,job,_=t.submit(timeout=3)
    started=time.monotonic();execute(t)
    assert time.monotonic()-started<8
    row,_,_=t.rows(job['job_id']);assert row['error_code']=='temporal_timeout'
    marker=next(t.env.workspace.rglob('request.decoder-started'))
    assert row['released_at'] is not None
    import ctypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    for pid in json.loads(marker.read_text()):
        handle=kernel.OpenProcess(0x1000,False,pid)
        if handle:
            try:
                code=ctypes.c_ulong();assert kernel.GetExitCodeProcess(handle,ctypes.byref(code))
                assert code.value!=259
            finally:kernel.CloseHandle(handle)


@pytest.mark.parametrize('audio',[False,True])
def test_actual_video_nonzero_pts_and_audio_excluded(temporal_worker,tmp_path,audio):
    t=temporal_worker;path=tmp_path/'source.mp4'
    t.tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=size=32x32:rate=5:duration=0.4',
        *(['-f','lavfi','-i','sine=frequency=440:duration=0.4','-map','0:v:0','-map','1:a:0','-c:a','aac'] if audio else []),
        '-vf','setpts=PTS+5/TB','-fps_mode','passthrough','-c:v','libx264','-threads','1','-pix_fmt','yuv420p',
        '-video_track_timescale','1000',str(path)],timeout=20)
    _,job,_=t.submit(path.read_bytes(),name='selected.mp4',kind='video');execute(t)
    row,asset,_=t.rows(job['job_id']);_,_,report,_=verified(t.env.router,row,asset)
    sequence=report.result.sequence
    assert sequence.source_origin.fraction()==5 and [frame.source_pts for frame in sequence.frames]==[5000,5200]
    assert sequence.audio_policy=='not_analyzed'
    sample=report.result.document.evidence[0]
    assert sample.start_seconds==(5. if audio else 0.)
    assert sample.end_seconds==(5.4 if audio else .4)
    assert report.result.source_clock_origin.fraction()==(0 if audio else 5)


def test_wrong_modality_and_disabled_or_missing_root(temporal_worker,tmp_path):
    t=temporal_worker;_,job,_=t.submit(kind='video');execute(t)
    row,_,_=t.rows(job['job_id']);assert row['error_code']=='temporal_modality_changed'
    assert Deployment().enabled is False
    with pytest.raises(ValueError):Deployment(enabled=True,model_root='relative')
    with pytest.raises(ValueError):Deployment(enabled=True,model_root=str(tmp_path/'missing'))


def rewrite(t,row,change):
    """Consistent isolated byte/hash corruption must still fail semantic checks."""
    from media_tag.analysis.contracts import AnalysisDocument
    from media_tag.contracts.models import TaskEnvelope
    from media_tag.temporal_topics.runtime import suggestions
    payload=json.loads(row['result_json'])
    main=next(ref for ref in payload['artifacts'] if ref['kind']=='temporal_router_report')
    main_path=t.env.workspace/main['ref'];report=json.loads(main_path.read_bytes())
    change(report)
    local=report['result'];report['prepared']['sequence']=local['sequence']
    manifest=local['manifest'];manifest_raw=json.dumps({'source':report['prepared']['source'],
        'sequence':local['sequence']},sort_keys=True).encode()
    (t.env.workspace/manifest['ref']).write_bytes(manifest_raw);manifest['sha256']=sha256(manifest_raw).hexdigest()
    report['prepared']['manifest']=dict(manifest);local['document']['evidence'][0]['artifact']=dict(manifest)
    raw_ref=local['raw_report'];raw_path=t.env.workspace/raw_ref['ref'];raw=json.loads(raw_path.read_bytes())
    raw['manifest']=dict(manifest);raw['generation']['input_sha256']=local['sequence']['input_sha256']
    raw_bytes=json.dumps(raw,ensure_ascii=False).encode();raw_path.write_bytes(raw_bytes)
    raw_ref['sha256']=sha256(raw_bytes).hexdigest()
    encoded=json.dumps(report,ensure_ascii=False).encode();main_path.write_bytes(encoded);main['sha256']=sha256(encoded).hexdigest()
    replaced={ref['ref']:ref for ref in (main,manifest,raw_ref)}
    payload['artifacts']=[replaced.get(ref['ref'],ref) for ref in payload['artifacts']]
    payload['evidence']=local['document']['evidence']
    task=TaskEnvelope.model_validate_json(row['envelope_json'])
    document=AnalysisDocument.model_validate_json(json.dumps(local['document']))
    payload['suggestions']=[item.model_dump(mode='json') for item in suggestions(document,task)]
    with t.env.database.transaction(write=True) as connection:
        connection.execute('UPDATE router_attempts SET result_json=? WHERE task_id=? AND lease_epoch=?',
            (json.dumps(payload),row['task_id'],row['lease_epoch']))
    return t.rows(row['job_id'])[0]


def test_consistent_manifest_rgba_forgery_fails_actual_source_decode(temporal_worker):
    t=temporal_worker;_,job,_=t.submit();execute(t);row,asset,_=t.rows(job['job_id'])
    row=rewrite(t,row,lambda report:report['result']['sequence']['frames'][0].update(display_rgba_sha256='a'*64))
    with pytest.raises(ValueError,match='not_decoded_from_source'):verified(t.env.router,row,asset)


def test_consistent_retimed_manifest_cannot_replace_actual_frame_clock(temporal_worker):
    t=temporal_worker;_,job,_=t.submit();execute(t);row,asset,_=t.rows(job['job_id'])
    def change(report):
        sequence=report['result']['sequence'];half=Rational.of('1/10').model_dump()
        sequence['frames'][0]['duration']=half
        sequence['frames'][1].update(duration=half,normalized_time=half,source_time=half)
        sequence['model_frame_sources']=[0,1];sequence['model_fps']=Rational.of(10).model_dump()
        sequence['processor_padding_frames']=0
        frames=tuple((t.env.workspace/frame['model_image']['ref']).read_bytes() for frame in sequence['frames'])
        sequence['input_sha256']=input_digest(frames,Rational.of(10),Rational.of('1/5'))
    row=rewrite(t,row,change)
    with pytest.raises(ValueError,match='not_decoded_from_source'):verified(t.env.router,row,asset)


def test_consistent_field_rewrite_fails_actual_raw_answer_normalization(temporal_worker):
    t=temporal_worker;_,job,_=t.submit();execute(t);row,asset,_=t.rows(job['job_id'])
    def change(report):
        field=next(field for field in report['result']['document']['fields'] if field['field']=='英文检索词')
        field['value']['values']=['invented replacement']
    row=rewrite(t,row,change)
    with pytest.raises(ValueError,match='normalization_changed'):verified(t.env.router,row,asset)


@pytest.mark.parametrize('state',['cancelled','unreleased','inactive'])
def test_current_attempt_and_release_are_required(temporal_worker,state):
    t=temporal_worker;_,job,_=t.submit();execute(t);row,asset,_=t.rows(job['job_id'])
    with t.env.database.transaction(write=True) as connection:
        if state=='cancelled':connection.execute('UPDATE router_jobs SET cancelled=1 WHERE job_id=?',(job['job_id'],))
        elif state=='inactive':connection.execute('UPDATE router_tasks SET active=0 WHERE task_id=?',(row['task_id'],))
        else:connection.execute('UPDATE router_attempts SET released_at=NULL WHERE task_id=?',(row['task_id'],))
    with pytest.raises(ValueError,match='current_attempt_changed'):verified(t.env.router,row,asset)


def test_new_failed_scope_does_not_fall_back_to_prior_success(temporal_worker):
    t=temporal_worker;_,job,spec=t.submit();execute(t)
    row,asset,job=t.rows(job['job_id'])
    new=spec.model_copy(update={'key':'temporal-next'})
    t.env.router.replan(job['job_id'],(new,),expected_revision=job['revision'],principal=t.env.owner)
    with t.env.database.transaction(write=True) as connection:
        connection.execute("UPDATE router_tasks SET state='needs_attention',error_code='synthetic_block' WHERE job_id=? AND step_key='temporal-next' AND active=1",(job['job_id'],))
    with t.env.database.transaction() as connection:
        job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(job['job_id'],)).fetchone()
        fields,_,_,states=merge(t.env.router,connection,asset,'animation',job,{},(),())
    assert all(field.value.state=='failed' for field in fields.values())
    assert states=={'temporal-next':'needs_attention'}


def test_oversized_report_is_rejected_before_any_generic_artifact_hash(temporal_worker,monkeypatch):
    t=temporal_worker;_,job,_=t.submit();execute(t);row,asset,_=t.rows(job['job_id'])
    ref=next(ref for ref in json.loads(row['result_json'])['artifacts'] if ref['kind']=='temporal_router_report')
    with (t.env.workspace/ref['ref']).open('ab') as stream:stream.write(b' '*2*1024**2)
    monkeypatch.setattr(t.env.router,'_artifacts',lambda *args:(_ for _ in ()).throw(AssertionError('unbounded generic hashing')))
    with pytest.raises(ValueError,match='artifact_budget'):verified(t.env.router,row,asset)


@pytest.mark.parametrize('mode,code',[('logs','temporal_log_budget'),('response','temporal_response_budget')])
def test_live_child_output_budgets_terminate_before_lease_release(temporal_worker,mode,code):
    t=temporal_worker;t.script.write_text(WIRE.replace('__MODE__',repr(mode)),encoding='utf8')
    _,job,_=t.submit();execute(t);row,_,_=t.rows(job['job_id'])
    assert row['error_code']==code and row['released_at'] is not None


def test_cancellation_terminates_decoder_before_returning(temporal_worker):
    from concurrent.futures import ThreadPoolExecutor
    t=temporal_worker;t.script.write_text(WIRE.replace('__MODE__',repr('decode-hang')),encoding='utf8')
    _,job,_=t.submit();assert t.executor.run_once()['accepted']
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(t.executor.run_once)
        deadline=time.monotonic()+5.
        while not list(t.env.workspace.rglob('request.decoder-started')):
            assert time.monotonic()<deadline
            time.sleep(.02)
        t.stops[-1].set();assert pending.result()['accepted']
    row,_,_=t.rows(job['job_id']);assert row['error_code']=='cancelled' and row['released_at'] is not None


def test_source_redecode_does_not_hold_a_sqlite_writer(temporal_worker,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import media_tag.temporal_topics.projection as projection
    t=temporal_worker;_,job,_=t.submit();execute(t);row,asset,_=t.rows(job['job_id'])
    entered=threading.Event();written=threading.Event();actual=projection.redecoded
    def slow(*args):
        entered.set();assert written.wait(5)
        return actual(*args)
    monkeypatch.setattr(projection,'redecoded',slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending=pool.submit(verified,t.env.router,row,asset)
        assert entered.wait(5)
        try:
            with t.env.database.transaction(write=True) as connection:
                connection.execute("INSERT INTO catalog_metadata VALUES('temporal-concurrent-write','ok')")
        finally:written.set()
        assert pending.result()[2].result.sequence.coverage=='one_complete_animation_cycle'


def test_current_model_configuration_change_invalidates_prior_success(temporal_worker):
    from dataclasses import replace
    t=temporal_worker;_,job,_=t.submit();execute(t);row,asset,_=t.rows(job['job_id'])
    definition=t.env.router.stages['temporal_topics']
    t.env.router.stages['temporal_topics']=replace(definition,required_model_revision='b'*64)
    with pytest.raises(ValueError,match='configuration_changed'):verified(t.env.router,row,asset)


def test_source_video_hdr_waits_in_child_before_model(temporal_worker,tmp_path):
    t=temporal_worker;path=tmp_path/'hdr.mp4'
    t.tools.ffmpeg_run(['-v','error','-f','lavfi','-i','testsrc2=size=32x32:rate=5:duration=0.4',
        '-c:v','libx264','-threads','1','-pix_fmt','yuv420p','-color_primaries','bt2020',
        '-color_trc','smpte2084','-colorspace','bt2020nc',
        '-video_track_timescale','1000',str(path)],timeout=20)
    actual=t.tools.probe_run(path,timeout=10,extra=['-show_streams'])['streams'][0]
    assert str(actual.get('color_space','')).startswith('bt2020')
    _,job,_=t.submit(path.read_bytes(),name='hdr.mp4',kind='video');execute(t)
    row,_,_=t.rows(job['job_id'])
    assert row['error_code']=='explicit_video_color_conversion_policy_required'


def test_processor_fingerprint_cannot_import_native_model_runtime():
    from media_tag.workers.probe import run_bounded
    code="from media_tag.temporal_topics.runtime import tool_digest;import sys;tool_digest(type('Tools',(),{'identity':{}})());assert 'torch' not in sys.modules;assert 'transformers' not in sys.modules;print('metadata-only')"
    output,_=run_bounded(Path(sys.executable),['-c',code],timeout=10,stdout_limit=1024,stderr_limit=16384)
    assert output.strip()==b'metadata-only'


def test_registered_source_byte_budget_is_checked_before_generic_open(temporal_worker,monkeypatch):
    t=temporal_worker;_,job,_=t.submit(options=Options(max_input_bytes=1))
    assert t.executor.run_once()['accepted']  # Independent technical probe.
    def forbidden(*args,**kwargs):raise AssertionError('generic source hash before budget')
    monkeypatch.setattr(t.env.router,'open_input',forbidden)
    assert t.executor.run_once()['accepted']
    row,_,_=t.rows(job['job_id']);assert row['error_code']=='temporal_input_budget'
