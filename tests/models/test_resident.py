import io
import json
import os
import sys
import subprocess
import threading

import pytest

from media_tag.models.contracts import ModelFailure
from media_tag.models.resident import ResidentCLIP, serve


def test_keeps_handle_between_requests_and_rejects_wrong_binding(monkeypatch):
    parent = os.getpid() + 1
    monkeypatch.setattr('media_tag.models.lifecycle.watch_parent', lambda pid: None)
    calls = []
    def fake(req, *, shared_clip):
        shared_clip.setdefault('model', object())
        shared_clip['loads'], shared_clip['load_seconds'] = 1, .2
        calls.append(shared_clip['model'])
        return {'source_sha256':req['source_sha256']}
    monkeypatch.setattr('media_tag.models.runner.execute', fake)
    def packet(i, rev):
        return dict(seq=i,request=dict(executor_pid=parent,kind='clip',device='cuda',workspace='w',
            model_root='m',model_revision=rev,options={},source_sha256=str(i),frames=[]))
    incoming = [packet(1,'r'),packet(2,'r'),packet(3,'changed')]
    writer = io.BytesIO()
    assert serve(parent,'w','m','r',io.BytesIO(b''.join(json.dumps(i).encode()+b'\n' for i in incoming)),writer)==2
    a,b,c = map(json.loads,writer.getvalue().splitlines())
    assert a['data'] != b['data'] and a['model_loads'] == b['model_loads'] == 1
    assert len(calls)==2 and calls[0] is calls[1] and c['error_code']=='model_result_binding'


@pytest.mark.parametrize('reason', ['cancelled','model_timeout'])
def test_stalled_resident_is_joined_after_cancel_or_timeout(tmp_path,reason):
    lane = ResidentCLIP(tmp_path,tmp_path,'r')
    p = subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'],stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    lane.process = p
    cancel=threading.Event()
    trigger=threading.Timer(.15,cancel.set)
    if reason=='cancelled':trigger.start()
    try:
        with pytest.raises(ModelFailure,match=reason):
            lane.run({},tmp_path,cancel,2 if reason=='cancelled' else .15)
        assert p.poll() is not None and lane.process is None
    finally:
        trigger.cancel()
        lane.close()


def test_real_private_child_rejects_mismatched_revision_before_gpu_load(tmp_path):
    lane = ResidentCLIP(tmp_path,tmp_path,'pinned')
    req = dict(executor_pid=os.getpid(),kind='clip',device='cuda',workspace=str(tmp_path),model_root=str(tmp_path),
               model_revision='different',options={},frames=[],source_sha256='0'*64)
    try:
        with pytest.raises(ModelFailure,match='model_result_binding'):
            lane.run(req,tmp_path,threading.Event(),10)
        response=json.loads((tmp_path/'response.json').read_bytes())
        assert response['seq']==1 and 'model_loads' not in response and lane.process is None
    finally:
        lane.close()
