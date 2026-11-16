"""One supervised CUDA CLIP process per local Worker. Only private pipes, no network."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from .contracts import ModelFailure

MAX_REQUEST = 256 * 1024
MAX_REPLY = 8 * 1024 * 1024


class ResidentCLIP:
    def __init__(self, workspace, model_root, revision):
        self.binding = (str(Path(workspace).resolve()), str(Path(model_root).resolve()), revision)
        self.process = None
        self.tree = None
        self.worker_pid = None
        self.sequence = 0

    def _start(self):
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                   TOKENIZERS_PARALLELISM='false', OMP_NUM_THREADS='1', PYTHONUTF8='1')
        flags = 0
        if os.name == 'nt':
            from media_tag.transforms.processes import ProcessTree
            self.tree = ProcessTree()
            flags = subprocess.CREATE_NO_WINDOW | 0x00000004  # CREATE_SUSPENDED: contain the venv launcher and interpreter.
        try:
            self.process = subprocess.Popen([sys.executable, '-m', 'media_tag.models.resident',
                str(os.getpid()), *self.binding], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                env=env, creationflags=flags)
            if self.tree:
                from media_tag.deployment.local import resume_owned_process
                self.tree.assign(self.process)
                resume_owned_process(self.process)
        except BaseException:
            self.close()
            raise
        self.sequence = 0
        self.worker_pid = None

    def close(self):
        p, self.process = self.process, None
        if self.tree is not None:
            self.tree.stop_and_close()
            self.tree = None
        if p is not None:
            if p.poll() is None:
                p.kill()
            p.wait()
            p.stdin.close()
            p.stdout.close()

    def run(self, request, output, stopped, timeout):
        if stopped.is_set():
            raise ModelFailure('cancelled')
        if self.process is None or self.process.poll() is not None:
            self.close()
            self._start()
        p = self.process
        self.sequence += 1
        seq = self.sequence
        outgoing = json.dumps({'seq': seq, 'request': request}, allow_nan=False).encode() + b'\n'
        if len(outgoing) > MAX_REQUEST:
            self.close()
            raise ModelFailure('frame_budget')
        (output/'request.json').write_text(json.dumps(request, sort_keys=True), encoding='utf-8')
        received, finished = [], threading.Event()

        def exchange():
            try:
                p.stdin.write(outgoing)
                p.stdin.flush()
                received.append(p.stdout.readline(MAX_REPLY + 1))
            except (ValueError, OSError):
                pass
            finally:
                finished.set()

        thread = threading.Thread(target=exchange, daemon=True)
        deadline = time.monotonic() + timeout
        thread.start()
        try:
            while not finished.wait(.04):
                if stopped.is_set():
                    raise ModelFailure('cancelled')
                if time.monotonic() >= deadline:
                    raise ModelFailure('model_timeout')
            if stopped.is_set():
                raise ModelFailure('cancelled')
            if not received or not received[0].endswith(b'\n') or len(received[0]) > MAX_REPLY:
                raise ModelFailure('model_process_failed')
            reply = json.loads(received[0])
            if (reply['seq'] != seq or type(reply['pid']) is not int or reply['pid'] <= 0
                    or (self.worker_pid is not None and reply['pid'] != self.worker_pid)):
                raise ModelFailure('model_result_binding')
            self.worker_pid = reply['pid']
            (output/'response.json').write_bytes(received[0])
            if not reply['ok']:
                raise ModelFailure(reply['error_code'])
            return reply
        except BaseException:
            self.close()
            raise
        finally:
            thread.join()


def serve(parent_pid, workspace, model_root, revision, reader, writer):
    from .runner import execute
    from .lifecycle import watch_parent
    watch_parent(parent_pid)
    shared = {}
    sequence = 0
    while True:
        line = reader.readline(MAX_REQUEST + 1)
        if not line:
            return 0
        if len(line) > MAX_REQUEST or not line.endswith(b'\n'):
            return 2
        sequence += 1
        try:
            packet = json.loads(line)
            r = packet['request']
            if (packet['seq'] != sequence or r['executor_pid'] != parent_pid or r['kind'] != 'clip'
                or r['device'] != 'cuda' or r['workspace'] != workspace or r['model_root'] != model_root
                or r['model_revision'] != revision or set(r) != {'executor_pid','kind','device','workspace',
                    'model_root','model_revision','options','source_sha256','frames'}):
                raise ModelFailure('model_result_binding')
            data = execute(r, shared_clip=shared)
            reply = dict(ok=True, data=data, model_loads=shared['loads'], load_seconds=shared['load_seconds'])
        except Exception as exc:
            code = exc.code if isinstance(exc, ModelFailure) else 'oom' if type(exc).__name__ in (
                'OutOfMemoryError','CUDAOutOfMemoryError') else 'model_execution_failed'
            reply = dict(ok=False, error_code=code)
        reply.update(pid=os.getpid(), seq=sequence)
        raw = json.dumps(reply, allow_nan=False).encode() + b'\n'
        if len(raw) > MAX_REPLY:
            return 2
        writer.write(raw)
        writer.flush()
        if not reply['ok']:
            return 2


if __name__ == '__main__':
    # Native libraries may print to stdout; isolate that stream from our pipe protocol.
    with os.fdopen(os.dup(sys.stdout.fileno()), 'wb') as wire:
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
        raise SystemExit(serve(int(sys.argv[1]), *sys.argv[2:], sys.stdin.buffer, wire))
