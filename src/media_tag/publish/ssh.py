"""Send single-item index requests through the operator's trusted SSH host."""
import json
from pathlib import PurePosixPath
import re
import shlex
import subprocess
from threading import Lock, Timer
from urllib.parse import urlsplit

import httpx


_REMOTE = r"""
import http.client, json, pathlib, sys
while packet := sys.stdin.buffer.readline(32*1024*1024+1):
    if len(packet) > 32*1024*1024 or not packet.endswith(b'\n'):
        raise ValueError('invalid request frame')
    r = json.loads(packet)
    token = pathlib.Path(r['token_file']).read_text().strip()
    c = http.client.HTTPConnection('127.0.0.1', r['port'], timeout=1200)
    try:
        c.request('POST', r['path'], r['body'].encode(), {'Content-Type':'application/json', 'Authorization':'Bearer '+token})
        response = c.getresponse()
        body = response.read(4194305)
        if len(body) > 4194304:
            raise ValueError('response too large')
        sys.stdout.buffer.write(f'{response.status} {len(body)}\n'.encode()+body)
        sys.stdout.buffer.flush()
    finally:
        c.close()
"""


class ItemTransport(httpx.BaseTransport):
    def __init__(self, host, origin, token_file):
        url = urlsplit(origin)
        if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', host)
                or url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                or not PurePosixPath(token_file).is_absolute()):
            raise ValueError('trusted SSH host, loopback origin and remote publisher token path required')
        self.host, self.port, self.token_file = host, url.port, token_file
        self._lock = Lock()
        self._process = None

    def _disconnect(self):
        process, self._process = self._process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait()
        finally:
            process.stdout.close()

    def close(self):
        with self._lock:
            self._disconnect()

    def handle_request(self, request):
        path = request.url.path
        allowed = r'/internal/releases(?:/item-[0-9a-f]{64}/(?:index-item|objects/[0-9a-f]{64}/upload-url))?'
        body = request.read()
        if request.method != 'POST' or not re.fullmatch(allowed, path) or request.url.query or len(body) > 4 * 1024**2:
            raise ValueError('unsupported item publication request')
        packet = json.dumps(dict(port=self.port, token_file=self.token_file, path=path, body=body.decode('utf-8')))
        with self._lock:
            try:
                if self._process is None:
                    args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                            '-o', 'ConnectTimeout=8', self.host, 'python3 -u -c ' + shlex.quote(_REMOTE)]
                    self._process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                process = self._process
                deadline = Timer(1220, process.kill)
                deadline.daemon = True
                deadline.start()
                try:
                    process.stdin.write(packet.encode()+b'\n')
                    process.stdin.flush()
                    header = re.fullmatch(rb'([1-5][0-9]{2}) ([0-9]{1,7})\n', process.stdout.readline(32))
                    if header is None or int(header[2]) > 4*1024**2:
                        raise ValueError('invalid response frame')
                    size = int(header[2])
                    raw = process.stdout.read(size)
                    if len(raw) != size:
                        raise ValueError('incomplete response frame')
                    return httpx.Response(int(header[1]), content=raw)
                finally:
                    deadline.cancel()
                    deadline.join()
            except (OSError, ValueError, subprocess.SubprocessError):
                self._disconnect()
                raise ValueError('SSH publisher connection failed') from None
