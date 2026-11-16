"""Send single-item index requests through the operator's trusted SSH host."""
import json
from pathlib import PurePosixPath
import re
import shlex
import subprocess
from urllib.parse import urlsplit

import httpx


_REMOTE = """
import http.client, json, pathlib, sys
r = json.load(sys.stdin)
token = pathlib.Path(r['token_file']).read_text().strip()
c = http.client.HTTPConnection('127.0.0.1', r['port'], timeout=1200)
c.request('POST', r['path'], r['body'].encode(), {'Content-Type':'application/json', 'Authorization':'Bearer '+token})
response = c.getresponse()
body = response.read(4194305)
if len(body) > 4194304:
    raise ValueError('response too large')
sys.stdout.buffer.write(str(response.status).encode()+b'\\n'+body)
"""


class ItemTransport(httpx.BaseTransport):
    def __init__(self, host, origin, token_file):
        url = urlsplit(origin)
        if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}', host)
                or url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                or not PurePosixPath(token_file).is_absolute()):
            raise ValueError('trusted SSH host, loopback origin and remote publisher token path required')
        self.host, self.port, self.token_file = host, url.port, token_file

    def handle_request(self, request):
        path = request.url.path
        allowed = r'/internal/releases(?:/item-[0-9a-f]{64}/(?:index-item|objects/[0-9a-f]{64}/upload-url))?'
        body = request.read()
        if request.method != 'POST' or not re.fullmatch(allowed, path) or request.url.query or len(body) > 4 * 1024**2:
            raise ValueError('unsupported item publication request')
        packet = json.dumps(dict(port=self.port, token_file=self.token_file, path=path, body=body.decode('utf-8')))
        args = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=8', self.host, 'python3 -c ' + shlex.quote(_REMOTE)]
        try:
            result = subprocess.run(args, input=packet.encode(), stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, timeout=1220, check=True,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            status, raw = result.stdout.split(b'\n', 1)
            return httpx.Response(int(status), content=raw)
        except (OSError, ValueError, subprocess.SubprocessError):
            raise ValueError('SSH publisher connection failed') from None
