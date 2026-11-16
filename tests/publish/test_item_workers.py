from contextlib import closing, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace

import httpx

from media_tag.publish import ssh
from media_tag.publish.items import ItemPublisher
from media_tag.api.config import ItemPublicationConfig, PreprocessBatch


def test_hot_reload_applies_workers_only_for_same_json_batch(tmp_path):
    batch = PreprocessBatch(owner_id='owner', upload_requests=('first', 'second'))
    options = ItemPublicationConfig(owner_id='owner', base_url='http://127.0.0.1:1234',
                                   token_file='/example', producer_revision='sample')
    publisher = ItemPublisher.__new__(ItemPublisher)
    publisher.options = options
    publisher.config = SimpleNamespace(preprocess_batch=batch)
    publisher._requests = json.dumps(batch.upload_requests)
    publisher.config_path = tmp_path/'runtime.json'
    for requests, expected in [(['first','second'], 2), (['first'], 1)]:
        publisher.config_path.write_text(json.dumps({
            'item_publication': options.model_dump(mode='json') | {'batch_workers': 2},
            'preprocess_batch': {'owner_id': 'owner', 'upload_requests': requests}}))
        publisher._checked_workers_at = 0
        assert publisher._workers() == expected


def test_framed_ssh_reuses_process_and_preserves_body_and_status(tmp_path, monkeypatch):
    # Run the same remote helper locally. The private token stays in its file.
    token = tmp_path / 'test-token'
    token.write_text('synthetic')
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            assert self.headers['Authorization'] == 'Bearer synthetic'
            body = self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(201)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *_):
            pass
    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        processes = []
        spawn = subprocess.Popen
        def local_remote(args, **kwargs):
            assert args[0]=='ssh' and 'StrictHostKeyChecking=yes' in args
            processes.append(spawn([sys.executable, '-u', '-c', ssh._REMOTE], **kwargs))
            return processes[-1]
        monkeypatch.setattr(ssh.subprocess, 'Popen', local_remote)
        url = f'http://127.0.0.1:{server.server_port}'
        transport = ssh.ItemTransport('test-host', url, '/test-token')
        transport.token_file = str(token)
        try:
            with httpx.Client(base_url=url,transport=transport) as client:
                for value in ('first\nrequest','请求二'):
                    body = value.encode()
                    reply = client.post('/internal/releases',content=body)
                    assert reply.status_code == 201 and reply.content == body
            assert len(processes)==1 and processes[0].poll()==0
        finally:
            server.shutdown()
            thread.join()


def test_parallel_selection_skips_busy_assets_and_unreleased_versions():
    with closing(sqlite3.connect(':memory:')) as db:
        db.row_factory=sqlite3.Row
        db.execute('CREATE TABLE item_deliveries(seq INTEGER PRIMARY KEY,asset_id,owner_id,state,retry_at)')
        db.executemany('INSERT INTO item_deliveries VALUES(?,?,?,?,?)',[
            (1,'a','owner','pending',1e12),(2,'a','owner','pending',0),
            (3,'b','owner','pending',0),(4,'c','owner','pending',0),(5,'d','owner','pending',0)])
        db.execute('CREATE TABLE uploads(asset_id,owner_id,state,request_id)')
        db.executemany('INSERT INTO uploads VALUES(?,?,?,?)',[
            ('a','owner','completed','one'),('c','owner','completed','two'),('d','other','completed','two')])
        publisher = ItemPublisher.__new__(ItemPublisher)
        publisher.owner=SimpleNamespace(actor_id='owner')
        publisher.router=SimpleNamespace(database=SimpleNamespace(transaction=lambda: nullcontext(db)))
        publisher._requests=json.dumps(['one','two'])
        assert publisher._next()['asset_id']=='b'
        assert publisher._next(['b'],batch_only=True)['asset_id']=='c'
        assert publisher._next(['b','c'],batch_only=True) is None
        db.execute("UPDATE item_deliveries SET state='indexed' WHERE seq=1")
        assert publisher._next(['b','c'],batch_only=True)['seq']==2
