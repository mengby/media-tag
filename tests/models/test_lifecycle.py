import os
from pathlib import Path
import subprocess
import sys
import time

import psutil


def test_actual_child_exits_when_executor_is_killed(tmp_path):
    marker = tmp_path / 'child.pid'
    child_code = "from media_tag.models.lifecycle import watch_parent; import os,pathlib,sys,time; watch_parent(int(sys.argv[1])); pathlib.Path(sys.argv[2]).write_text(str(os.getpid())); time.sleep(60)"
    parent_code = "import os,subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],str(os.getpid()),sys.argv[2]]); time.sleep(60)"
    parent = subprocess.Popen([sys.executable, '-c', parent_code, child_code, str(marker)],
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    child = None
    try:
        deadline = time.monotonic() + 8
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert marker.exists()
        child = psutil.Process(int(marker.read_text()))
        assert child.is_running()
        parent.kill(); parent.wait(timeout=5)
        assert child.wait(timeout=5) == 3
    finally:
        if parent.poll() is None:
            parent.kill(); parent.wait(timeout=5)
        if child and child.is_running():
            child.kill(); child.wait(timeout=5)
