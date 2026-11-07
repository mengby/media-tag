from contextlib import contextmanager
from hashlib import sha256
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from media_tag.api.app import create_app
from media_tag.catalog import Principal
from media_tag.upload.service import UploadService
from media_tag.variants.service import VariantService
from router.test_api import TestClient
from .conftest import spec


def app(env):
    bindings = {sha256(f'synthetic-{name}-token'.encode()).hexdigest(): principal
                for name, principal in [('owner', env.owner), ('worker', env.workers[0]), ('other', Principal(actor_id='synthetic-other'))]}
    return create_app(env.router, bindings, uploads=env.uploads, variants=VariantService(env.router, env.uploads.tools))


def headers(role='owner', **kwargs):
    return {'Authorization': f'Bearer synthetic-{role}-token', **kwargs}


def test_http_upload_auth_body_limits_redaction_and_cancel(intake):
    env = intake
    with TestClient(app(env)) as client:
        model = spec(env).model_dump(mode='json')
        assert client.post('/uploads', json=model).status_code == 401
        assert client.post('/uploads', json=model, headers=headers('worker')).status_code == 403
        assert client.post('/uploads', json={**model, 'owner_id': 'admin', 'token': 'secret-synthetic'}, headers=headers()).status_code == 422
        created = client.post('/uploads', json=model, headers=headers()).json()
        url = '/uploads/' + created['upload_id']
        assert client.get(url, headers=headers('other')).status_code == 403
        assert client.put(url + '/chunks?offset=0', content=env.data, headers=headers()).status_code == 422
        good = headers(**{'X-Chunk-SHA256': sha256(env.data).hexdigest()})
        assert client.put(url + '/chunks?offset=not-an-int-secret', content=env.data, headers=good).status_code == 422
        bad = client.put(url + '/chunks?offset=not-an-int-secret', content=env.data, headers=good)
        assert 'not-an-int-secret' not in bad.text
        assert client.put(url + '/chunks?offset=0', content=b'x' * 1025, headers=good).status_code == 413
        assert client.put(url + '/chunks?offset=0', content=env.data, headers=good).json()['offset'] == len(env.data)
        assert client.post(url + '/cancel', headers=headers()).status_code == 200
        assert client.post(url + '/complete', headers=headers()).status_code == 409
        for path in ('/variants/register', '/uploads/private/input.bin', '/assets/arbitrary/download'):
            assert client.get(path, headers=headers()).status_code == 404


@contextmanager
def running(application):
    listener = socket.socket(); listener.bind(('127.0.0.1', 0))
    server = uvicorn.Server(uvicorn.Config(application, access_log=False, log_level='critical'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True); thread.start()
    try:
        deadline = time.monotonic() + 8
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        with httpx.Client(base_url=f'http://127.0.0.1:{listener.getsockname()[1]}', timeout=10) as client:
            yield client
    finally:
        server.should_exit = True; thread.join(8); listener.close(); assert not thread.is_alive()


def test_real_http_server_restart_resume_finalize_and_background_job(intake):
    env = intake
    with running(app(env)) as client:
        created = client.post('/uploads', json=spec(env).model_dump(mode='json'), headers=headers()).json()
        url = '/uploads/' + created['upload_id']
        first = env.data[:10]
        assert client.put(url + '/chunks?offset=0', content=first, headers=headers(**{'X-Chunk-SHA256': sha256(first).hexdigest()})).json()['offset'] == 10
    env.uploads = UploadService(env.router, env.uploads.tools, options=env.uploads.options)
    with running(app(env)) as client:
        assert client.get(url, headers=headers()).json()['offset'] == 10
        last = env.data[10:]
        assert client.put(url + '/chunks?offset=10', content=last, headers=headers(**{'X-Chunk-SHA256': sha256(last).hexdigest()})).status_code == 200
        response = client.post(url + '/complete', headers=headers())
        assert response.status_code == 200
        complete = response.json()
        assert complete['job_id'] and complete['processing_history'] == 'external_unknown'
        listing = client.get(f"/assets/{complete['asset_id']}/variants", headers=headers()).json()
        assert len(listing) == 1 and listing[0]['asset_id'] == complete['asset_id']
