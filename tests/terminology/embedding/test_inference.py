import math
from pathlib import Path
import threading
import time
import pytest
from media_tag.models.contracts import ModelFailure
from media_tag.terminology.embedding.registry import BGERegistry
from media_tag.terminology.embedding.process import ProcessEncoder


@pytest.fixture
def encoder(tmp_path):
    registry=BGERegistry(Path('cache/models/d14').resolve());registry.verify()
    model=ProcessEncoder(registry,tmp_path)
    try:yield model
    finally:model.close()


def test_real_chinese_cpu_cls_normalization_reuse_and_no_silent_truncation(encoder):
    rows=encoder.encode(['红色的圆形','蓝色的方形'],timeout_seconds=30.0)
    pid=encoder.process.pid
    query=encoder.encode(['红色圆圈'],query=True,timeout_seconds=8.0)[0]
    assert encoder.process.pid==pid
    assert all(len(v)==512 and abs(sum(x*x for x in v)-1)<.0001 for v in [*rows,query])
    scores=[sum(a*b for a,b in zip(query,row)) for row in rows]
    assert scores[0]>scores[1] and encoder.stats['device']=='cpu'
    with pytest.raises(ModelFailure,match='text_token_budget'):
        encoder.encode(['不'+('圆形'*400)+'没有加重'],query=True,timeout_seconds=8.0)
    assert encoder.process is None


def test_actual_process_timeout_and_cancellation_leave_no_child(encoder):
    with pytest.raises(ModelFailure,match='text_model_timeout'):encoder.encode(['测试'],timeout_seconds=.0001)
    assert encoder.process is None
    stopped=threading.Event();failures=[]
    def execute():
        try:encoder.encode(['测试取消'],timeout_seconds=30.0,stopped=stopped)
        except ModelFailure as exc:failures.append(exc.code)
    thread=threading.Thread(target=execute);thread.start()
    deadline=time.monotonic()+5
    while encoder.process is None and time.monotonic()<deadline:time.sleep(.005)
    child=encoder.process
    assert child is not None
    stopped.set();thread.join(10)
    assert not thread.is_alive() and failures==['cancelled'] and child.poll() is not None and encoder.process is None
