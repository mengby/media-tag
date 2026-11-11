"""Private offline runner: all full-sequence decoding happens below the start gate."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sys
import time

from media_tag.models.contracts import ModelFailure
from media_tag.models.lifecycle import watch_parent
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import Toolchain,MediaError
from .contracts import Generation,Options,Pending,Rational
from .clocks import check_window
from .native import LocalQwenVideoGenerator
from .prompt import PROMPT_VERSION
from .service import TemporalTopicService


def execute(request):
    options=Options.model_validate(request['options'])
    if request['prompt_version']!=PROMPT_VERSION:raise ModelFailure('temporal_prompt_changed')
    output=Path(request['output']);source=Path(request['source_path'])
    root=Path(request['artifact_root'])
    if not all(path.is_absolute() for path in (output,source,root)):
        raise ModelFailure('temporal_absolute_paths_required')
    tools=Toolchain(Path(request['tool_dir']))
    if sha256(json.dumps(tools.identity,sort_keys=True).encode()).hexdigest()!=request['tool_identity']:
        raise ModelFailure('temporal_tool_changed')
    deadline=time.monotonic()+request['timeout_seconds']
    class BoundedTools:
        identity=tools.identity
        def probe_run(self,*args,timeout,**kwargs):
            return tools.probe_run(*args,timeout=min(timeout,max(.01,deadline-time.monotonic())),**kwargs)
        def ffmpeg_run(self,*args,timeout,**kwargs):
            return tools.ffmpeg_run(*args,timeout=min(timeout,max(.01,deadline-time.monotonic())),**kwargs)
    service=TemporalTopicService(output,policy=None,generator=None,tools=BoundedTools(),options=options,artifact_root=root)
    with FilesystemBackend(source.parent).open_read(source.name) as stream:raw=stream.read(options.max_input_bytes+1)
    if len(raw)>options.max_input_bytes or sha256(raw).hexdigest()!=request['source_sha256']:
        raise ModelFailure('temporal_source_integrity')
    prepared,frames=service.prepare(raw,expected_source_sha256=request['source_sha256'],content_kind=request['content_kind'])
    if request['mode']=='decode':return {'ok':True,'prepared':prepared.model_dump(mode='json')}
    if request['mode']!='infer':raise ModelFailure('temporal_unknown_mode')
    check_window(prepared.sequence,Rational.model_validate_json(json.dumps(request['source_clock_origin'])),request['source_clock_span'])
    generator=LocalQwenVideoGenerator(request['model_root'],enabled=True)
    if generator.model_revision!=request['model_revision']:raise ModelFailure('temporal_model_changed')
    generation=generator(frame_bytes=frames,fps=prepared.sequence.model_fps,duration=prepared.sequence.duration,
        input_sha256=prepared.sequence.input_sha256,field_prompts=tuple(request['field_prompts']),options=options)
    if not isinstance(generation,Generation):raise ModelFailure('temporal_generation_required')
    return {'ok':True,'prepared':prepared.model_dump(mode='json'),'generation':generation.model_dump(mode='json')}


def main():
    watch_parent(os.getppid())
    request_path,response_path=map(Path,sys.argv[1:3])
    if request_path.stat().st_size>512*1024:return 2
    with request_path.open('rb') as stream:raw=stream.read(512*1024+1)
    if len(raw)>512*1024:return 2
    request=json.loads(raw);gate=Path(request['start_gate'])
    if gate.parent!=request_path.parent or response_path.parent!=request_path.parent:return 2
    output=Path(request['output']);source=Path(request['source_path'])
    if request_path.parent not in output.parents or request_path.parent not in source.parents:return 2
    deadline=time.monotonic()+10.
    while not gate.is_file():
        if time.monotonic()>=deadline:return 2
        time.sleep(.01)
    try:response=execute(request)
    except Exception as exc:
        response={'ok':False,'error_code':exc.code if isinstance(exc,(Pending,ModelFailure,MediaError)) else 'temporal_model_failed'}
    encoded=json.dumps(response,allow_nan=False)
    if len(encoded.encode())>1024**2:return 2
    response_path.write_text(encoded,encoding='utf8')
    return 0 if response['ok'] else 2


if __name__=='__main__':raise SystemExit(main())
