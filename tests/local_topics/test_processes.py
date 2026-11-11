"""Actual isolated sleeping processes; never native/model execution."""
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from media_tag.local_topics.contracts import LocalTopicDeployment
from media_tag.local_topics.runtime import LocalTopicHandlers
from media_tag.models.contracts import ModelFailure


def handler(tmp_path):
    return LocalTopicHandlers(None,None,LocalTopicDeployment(model_root=str(tmp_path)))


def test_assignment_failure_kills_unassigned_root(tmp_path,monkeypatch):
    import media_tag.local_topics.runtime as runtime
    original_popen = subprocess.Popen
    processes = []
    def start(command,**kwargs):
        process = original_popen([sys.executable,'-c','import time;time.sleep(60)'],**kwargs)
        processes.append(process);return process
    class RejectedTree:
        def assign(self,process):raise RuntimeError('synthetic_assignment_failure')
        def stop_and_close(self):pass
    monkeypatch.setattr(runtime.subprocess,'Popen',start)
    monkeypatch.setattr(runtime,'ProcessTree',RejectedTree)
    with pytest.raises(RuntimeError,match='assignment_failure'):
        handler(tmp_path).invoke({},tmp_path,None,threading.Event(),deadline=time.monotonic()+5)
    assert len(processes)==1 and processes[0].poll() is not None
    assert not (tmp_path/'start.ready').exists()


def test_tree_timeout_stops_root_and_child_after_gate(tmp_path,monkeypatch):
    import media_tag.local_topics.runtime as runtime
    original_popen = subprocess.Popen
    processes = []
    script = '''import json, pathlib, subprocess, sys, time
request=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf8'))
while not pathlib.Path(request['start_gate']).is_file(): time.sleep(.01)
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])
pathlib.Path(sys.argv[2]).write_text(str(child.pid))
time.sleep(60)
'''
    marker = tmp_path/'child.pid'
    def start(command,**kwargs):
        process=original_popen([sys.executable,'-c',script,command[-2],str(marker)],**kwargs)
        processes.append(process);return process
    monkeypatch.setattr(runtime.subprocess,'Popen',start)
    request={'start_gate':str(tmp_path/'start.ready')}
    with pytest.raises(ModelFailure) as error:
        handler(tmp_path).invoke(request,tmp_path,None,threading.Event(),deadline=time.monotonic()+1.5)
    assert error.value.code=='local_topic_timeout'
    assert processes[0].poll() is not None
    assert marker.is_file(), 'Synthetic child must have started after assignment gate.'
    pid=int(marker.read_text())
    if os.name=='nt':
        import ctypes
        from ctypes import wintypes
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        kernel.OpenProcess.restype=wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        kernel.WaitForSingleObject.restype=wintypes.DWORD
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        opened=kernel.OpenProcess(0x00100000,False,pid)
        if opened:
            try:assert kernel.WaitForSingleObject(opened,2000)==0
            finally:kernel.CloseHandle(opened)
    else:
        # Linux child can briefly be a reaped-later zombie; it must not be live.
        status=Path(f'/proc/{pid}/stat')
        assert not status.exists() or status.read_text().split()[2]=='Z'
