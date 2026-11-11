"""One bounded child owns decoding and GPU execution until the lease is released."""
import json
import os
import subprocess
import sys
import time

from media_tag.models.contracts import ModelFailure
from media_tag.transforms.processes import ProcessTree


def run_child(request, output, stopped, *, deadline, command=None):
    request_path,response_path=output/'request.json',output/'response.json'
    gate=output/'start.ready'
    if gate.exists() or response_path.exists() or request_path.exists():raise ModelFailure('temporal_attempt_output_exists')
    request=dict(request,start_gate=str(gate))
    encoded=json.dumps(request,ensure_ascii=False,allow_nan=False)
    if len(encoded.encode())>512*1024:raise ModelFailure('temporal_request_budget')
    request_path.write_text(encoded,encoding='utf8')
    allowed=('SYSTEMROOT','WINDIR','PATH','TEMP','TMP','VIRTUAL_ENV','PYTHONPATH')
    env={key:value for key,value in os.environ.items() if key.upper() in allowed}
    env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
        TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='1',PYTHONUTF8='1')
    argv=(command(request_path,response_path) if command is not None else
        [sys.executable,'-m','media_tag.temporal_topics.runner',str(request_path),str(response_path)])
    tree,process=ProcessTree(),None
    try:
        with (output/'native.stdout.log').open('wb') as stdout,(output/'native.stderr.log').open('wb') as stderr:
            process=subprocess.Popen(argv,env=env,stdin=subprocess.DEVNULL,stdout=stdout,stderr=stderr,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            tree.assign(process)
            gate.write_text('assigned\n')
            while process.poll() is None:
                if stopped.wait(.05):raise ModelFailure('cancelled')
                if time.monotonic()>=deadline:raise ModelFailure('temporal_timeout')
                if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size>2*1024**2:
                    raise ModelFailure('temporal_log_budget')
                if response_path.exists() and response_path.stat().st_size>1024**2:
                    raise ModelFailure('temporal_response_budget')
            if os.fstat(stdout.fileno()).st_size+os.fstat(stderr.fileno()).st_size>2*1024**2:
                raise ModelFailure('temporal_log_budget')
    finally:
        tree.stop_and_close()
        if process is not None:
            if process.poll() is None:process.kill()
            process.wait()
    if not response_path.is_file():raise ModelFailure('temporal_process_failed')
    if response_path.stat().st_size>1024**2:raise ModelFailure('temporal_response_budget')
    with response_path.open('rb') as stream:raw=stream.read(1024**2+1)
    if len(raw)>1024**2:raise ModelFailure('temporal_response_budget')
    try:response=json.loads(raw)
    except (ValueError,UnicodeError):raise ModelFailure('temporal_invalid_response') from None
    if not isinstance(response,dict) or type(response.get('ok')) is not bool:raise ModelFailure('temporal_invalid_response')
    if response['ok'] is not True:
        code=response.get('error_code')
        raise ModelFailure(code if isinstance(code,str) and 1<=len(code)<=100 and all(c.islower() or c.isdigit() or c=='_' for c in code)
            else 'temporal_process_failed')
    if process.returncode!=0:raise ModelFailure('temporal_process_failed')
    return response
