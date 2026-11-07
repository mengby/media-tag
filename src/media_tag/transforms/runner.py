"""Private D10 subprocess entry, gated until its executor assigns a Windows job."""
import json
from pathlib import Path
import sys
import time

from media_tag.models.contracts import ModelFailure
from media_tag.models.lifecycle import watch_parent
from media_tag.models.registry import ModelRegistry
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.probe import Toolchain, MediaError
from media_tag.workers.face_track import scan
from .models import ScanOptions,VideoOptions
from .redaction import render
from .retime import retime


def execute(request):
    tools=Toolchain(Path(request['tool_dir']))
    registry=ModelRegistry(Path(request['model_root'])) if request['model_root'] else None
    if (registry.revision if registry else None)!=request['model_revision']:
        raise ModelFailure('model_revision_mismatch')
    backend=FilesystemBackend(Path(request['source_root']))
    with backend.open_verified(request['source_key'],request['source_sha256'],request['source_bytes']) as stream:
        source=Path(stream.name)
        started=time.monotonic(); output=Path(request['output'])
        if request['kind']=='face_track':
            data=scan(source,output,tools,registry,request['contract'],ScanOptions.model_validate_json(json.dumps(request['options'])))
        elif request['kind']=='redaction_render':
            data=render(source,output,tools,request['revision'],request['contract'],request['parameters'])
        elif request['kind']=='retime_render':
            data=retime(source,output,tools,request['contract'],VideoOptions.model_validate_json(json.dumps(request['options'])))
        else: raise ModelFailure('stage_unavailable')
        if data['source_sha256']!=request['source_sha256']:
            raise ModelFailure('source_hash_mismatch')
        data.update(elapsed_seconds=time.monotonic()-started,peak_process_bytes=peak_memory())
        return data


def peak_memory():
    # Core-only retiming does not require psutil or a vision environment.
    import ctypes as c
    from ctypes import wintypes as w
    class Counters(c.Structure):
        _fields_=[('size',w.DWORD),('faults',w.DWORD)]+[(name,c.c_size_t) for name in
            ('peak_working_set','working_set','paged_peak','paged','nonpaged_peak','nonpaged','pagefile','peak_pagefile')]
    kernel=c.WinDLL('kernel32',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.K32GetProcessMemoryInfo.argtypes=[w.HANDLE,c.c_void_p,w.DWORD]
    value=Counters(); value.size=c.sizeof(value)
    if not kernel.K32GetProcessMemoryInfo(kernel.GetCurrentProcess(),c.byref(value),value.size):
        raise ModelFailure('process_memory_unavailable')
    return value.peak_working_set


def main(argv=None):
    arguments=sys.argv[1:] if argv is None else argv
    if len(arguments)!=2: return 2
    request_path,response_path=map(Path,arguments)
    try:
        request=json.loads(request_path.read_text(encoding='utf-8'))
        watch_parent(request['executor_pid'])
        deadline=time.monotonic()+10
        while not Path(request['start_gate']).is_file():
            if time.monotonic()>deadline: raise ModelFailure('process_tree_start_gate_timeout')
            time.sleep(.01)
        response={'ok':True,'data':execute(request)}
    except Exception as exc:
        response={'ok':False,'error_code':exc.code if isinstance(exc,(ModelFailure,MediaError)) else 'redaction_execution_failed'}
    response_path.write_text(json.dumps(response,allow_nan=False,sort_keys=True),encoding='utf-8')
    return 0 if response['ok'] else 2


if __name__=='__main__':
    raise SystemExit(main())
