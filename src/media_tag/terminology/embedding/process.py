"""Reusable CPU process; queueing, cold load, IPC and inference share one deadline."""
from collections import deque
import json
import os
import queue
import subprocess
import sys
import threading
import time
from uuid import uuid4
from media_tag.models.contracts import ModelFailure
from media_tag.workers.evidence import _directory


class ProcessEncoder:
    def __init__(self,registry,workspace):
        self.registry,self.workspace=registry,workspace
        self.lock=threading.Lock();self.process=None;self.stats={};self.reader=None;self.logs=None

    def _start(self):
        folder=_directory(self.workspace,'jobs/text-runtime-'+uuid4().hex)
        (folder/'README.md').write_text('Private resident D14 CPU runtime. No query text is logged here.\n',encoding='utf-8')
        (folder/'inputs.json').write_text(json.dumps({'model_revision':self.registry.revision,'device':'cpu'}),encoding='utf-8')
        self.logs=(folder/'native.stderr.log').open('wb')
        env=os.environ.copy();env.update(HF_HUB_OFFLINE='1',TRANSFORMERS_OFFLINE='1',HF_HUB_DISABLE_TELEMETRY='1',
            TOKENIZERS_PARALLELISM='false',OMP_NUM_THREADS='2',PYTHONUTF8='1')
        try:
            self.process=subprocess.Popen([sys.executable,'-m','media_tag.terminology.embedding.runner','--root',str(self.registry.root.resolve()),
                '--parent',str(os.getpid()),'--revision',self.registry.revision],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                stderr=self.logs,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except BaseException:
            self.logs.close();self.logs=None;raise
        self.responses=queue.Queue(maxsize=1);process=self.process;responses=self.responses
        def read():
            try:
                while True:
                    line=process.stdout.readline(8*1024**2+1)
                    if not line or len(line)>8*1024**2:break
                    try:responses.put_nowait(json.loads(line))
                    except (ValueError,queue.Full):break
            finally:
                try:responses.put_nowait(None)
                except queue.Full:pass
        self.reader=threading.Thread(target=read,name='d14-encoder-output',daemon=True);self.reader.start()

    def _stop(self):
        process=self.process
        if process is not None:
            if process.poll() is None:process.kill()
            process.wait()
            for stream in (process.stdin,process.stdout):
                if stream:stream.close()
            if self.reader:self.reader.join(timeout=3)
            self.process=None
        if self.logs:self.logs.close();self.logs=None

    def close(self):
        with self.lock:self._stop()

    def encode(self,texts,*,query=False,timeout_seconds=8.0,stopped=None):
        deadline=time.monotonic()+timeout_seconds
        def check():
            if stopped is not None and stopped.is_set():raise ModelFailure('cancelled')
            if time.monotonic()>=deadline:raise ModelFailure('text_model_timeout')
        acquired=False;writer=None
        try:
            while not acquired:
                check();acquired=self.lock.acquire(timeout=min(.025,max(.001,deadline-time.monotonic())))
            if self.process is not None and self.process.poll() is not None:self._stop()
            if self.process is None:self._start()
            check();request_id=uuid4().hex
            raw=(json.dumps({'id':request_id,'texts':list(texts),'query':query},ensure_ascii=True)+'\n').encode()
            if len(raw)>256*1024:raise ModelFailure('text_input_budget')
            process=self.process;write_errors=[]
            def write():
                try:process.stdin.write(raw);process.stdin.flush()
                except (OSError,ValueError) as exc:write_errors.append(type(exc).__name__)
            writer=threading.Thread(target=write,name='d14-encoder-input',daemon=True);writer.start()
            while True:
                check()
                if write_errors:raise ModelFailure('text_model_process_failed')
                if self.logs and os.fstat(self.logs.fileno()).st_size>2*1024**2:raise ModelFailure('text_model_log_budget')
                try:response=self.responses.get(timeout=min(.025,max(.001,deadline-time.monotonic())))
                except queue.Empty:continue
                if not response or response.get('id')!=request_id:raise ModelFailure('text_model_process_failed')
                if not response.get('ok'):raise ModelFailure(response.get('error','text_model_execution_failed'))
                if response.get('model_revision')!=self.registry.revision or response.get('device')!='cpu':raise ModelFailure('text_model_binding')
                self.stats={k:response[k] for k in ('model_revision','device','elapsed_seconds','peak_process_bytes')}
                return response['vectors']
        except BaseException:
            if acquired:self._stop()
            raise
        finally:
            if writer:writer.join(timeout=3)
            if acquired:self.lock.release()
