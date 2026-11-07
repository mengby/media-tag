from contextlib import contextmanager
from hashlib import sha256
import io

import anyio
import pytest

from media_tag.catalog import AccessDenied, Principal
from media_tag.files.http import transfer
from media_tag.files.service import FileError, FileService
from router.test_api import TestClient
from router.conftest import submit, step
from starlette.requests import Request
from .conftest import app, content, headers


@pytest.mark.parametrize('range_header,expected', [('bytes=0-3', b'synt'), ('bytes=4-', b'hetic source bytes'),
                                                 ('bytes=-5', b'bytes'), ('bytes=0-999', b'synthetic source bytes')])
def test_single_ranges_and_etag(env, range_header, expected):
    with TestClient(app(env)) as client:
        response = client.get(content(env.asset), headers=headers(Range=range_header))
        assert response.status_code == 206 and response.content == expected
        assert int(response.headers['content-length']) == len(expected)
        assert response.headers['etag'] == f'"{env.asset.sha256}"'
        assert response.headers['content-range'].endswith('/22')


@pytest.mark.parametrize('bad', ['bytes=22-', 'bytes=100-200', 'bytes=4-2', 'bytes=-0', 'bytes=-', 'bytes=bad', 'bytes=' + '9'*300 + '-'])
def test_invalid_or_unsatisfiable_ranges(env, bad):
    with TestClient(app(env)) as client:
        response = client.get(content(env.asset), headers=headers(Range=bad))
        assert response.status_code == 416
        assert response.headers['content-range'] == 'bytes */22'
        assert response.json()['error']['code'] == 'range_not_satisfiable'


def test_ignored_multi_ranges_head_conditions_and_if_range(env):
    with TestClient(app(env)) as client:
        url = content(env.asset); etag = f'"{env.asset.sha256}"'
        for header in ['bytes=0-1,4-5', 'frames=0-1']:
            assert client.get(url, headers=headers(Range=header)).status_code == 200
        head = client.request('HEAD', url, headers=headers(Range='bytes=0-1'))
        assert head.status_code == 200 and head.content == b'' and head.headers['content-length'] == '22'
        assert client.get(url, headers=headers(**{'If-None-Match': 'W/'+etag})).status_code == 304
        assert client.get(url, headers=headers(**{'If-Match': 'W/'+etag})).status_code == 412
        assert client.get(url, headers=headers(**{'If-Match': '*'})).status_code == 200
        assert client.get(url, headers=headers(**{'Range': 'bytes=3-', 'If-Range': etag})).status_code == 206
        full = client.get(url, headers=headers(**{'Range': 'bytes=3-', 'If-Range': '"old"'}))
        assert full.status_code == 200 and full.content == b'synthetic source bytes'
        assert client.get(url, headers=headers(**{'If-Match': '"old"', 'If-None-Match': etag})).status_code == 412


def test_authorization_before_hash_range_and_cache_checks(env):
    with TestClient(app(env)) as client:
        url = content(env.asset)
        assert client.get(url).status_code == 401
        for credential in ['other', 'worker']:
            result = client.get(url, headers=headers(credential, **{'If-None-Match': '*', 'Range': 'bytes=bad'}))
            assert result.status_code == 403
        wrong = url.replace(env.asset.sha256, 'f'*64)
        assert client.get(wrong, headers=headers()).status_code == 412
        assert client.get(f'/v1/assets/{env.asset.asset_id}/content', headers=headers()).status_code == 422
        assert client.get('/v1/assets/absent/content?sha256=' + 'a'*64, headers=headers()).status_code == 404
        for response in [client.get(url), client.get(url, headers=headers()), client.get(wrong, headers=headers())]:
            assert response.headers['cache-control'] == 'private, no-store'
            assert response.headers['vary'] == 'Authorization' and response.headers['x-content-type-options'] == 'nosniff'


def test_reader_cannot_write_even_when_it_owns_the_object(env):
    job = submit(env)
    reader = Principal(actor_id=env.owner.actor_id, role='reader')
    with pytest.raises(AccessDenied):
        env.catalog.revise(env.asset.asset_id, principal=reader, expected_revision=1, reason='forbidden', name='forbidden')
    with TestClient(app(env)) as client:
        assert client.get(f'/v1/assets/{env.asset.asset_id}', headers=headers()).status_code == 200
        for suffix in ('pause', 'cancel', 'replan'):
            payload = {'paused': True} if suffix == 'pause' else {'expected_revision': 1, 'steps': [step().model_dump(mode='json')]}
            assert client.post(f"/jobs/{job['job_id']}/{suffix}", json=payload, headers=headers()).status_code == 403
        assert client.post('/worker/claim', headers=headers()).status_code == 403
        assert client.post('/admin/recover', headers=headers()).status_code == 403


def test_withdrawal_or_read_revoke_refuses_old_manifests(env):
    other = Principal(actor_id='synthetic-file-other', role='reader')
    env.catalog.change_access(env.asset.asset_id, principal=env.owner, expected_revision=1, subject_id=other.actor_id, permission='read', reason='synthetic grant')
    with TestClient(app(env)) as client:
        manifest = client.get(content(env.asset, 'download-manifest'), headers=headers('other')).json()
        env.catalog.change_access(env.asset.asset_id, principal=env.owner, expected_revision=2, subject_id=other.actor_id, permission='read', reason='synthetic revoke', revoke=True)
        assert client.get(manifest['content'], headers=headers('other', **{'If-None-Match': manifest['etag']})).status_code == 403
        env.catalog.archive(env.asset.asset_id, principal=env.owner, expected_revision=3, reason='synthetic withdrawal')
        assert client.get(manifest['content'], headers=headers()).status_code == 404


@pytest.mark.parametrize('failure', ['missing', 'corrupt', 'backend_unconfigured', 'backend_raises'])
def test_unavailable_content_never_falls_back_to_source_or_cache(env, failure):
    path = env.objects / f'objects/{env.asset.sha256[:2]}/{env.asset.sha256}'
    if failure == 'missing': path.unlink()
    elif failure == 'corrupt': path.write_bytes(b'changed')
    elif failure == 'backend_unconfigured': env.catalog.backends.clear()
    else:
        from media_tag.storage.oss import OssBackend
        env.catalog.backends[env.catalog.backend_id] = OssBackend()
    with TestClient(app(env)) as client:
        for method in ('GET', 'HEAD'):
            response = client.request(method, content(env.asset), headers=headers(**{'If-None-Match': '*'}))
            assert response.status_code == 503
        assert client.get(content(env.asset, 'download-manifest'), headers=headers()).status_code == 503
    assert (env.originals/'synthetic.bin').read_bytes() == b'synthetic source bytes'


def test_details_and_headers_do_not_leak_paths_or_accept_filename_injection(env):
    env.catalog.revise(env.asset.asset_id, principal=env.owner, expected_revision=1, reason='synthetic filename', name='../../bad\r\nX-Injection: yes.exe')
    with TestClient(app(env)) as client:
        detail = client.get(f'/v1/assets/{env.asset.asset_id}', headers=headers()).json()
        assert 'source_key' not in str(detail) and 'backend_id' not in str(detail)
        manifest = client.get(content(env.asset, 'download-manifest'), headers=headers(Host='evil.invalid')).json()
        assert manifest['content'].startswith('/v1/') and 'evil.invalid' not in str(manifest)
        response = client.get(manifest['content'], headers=headers())
        assert 'x-injection' not in response.headers
        disposition = response.headers['content-disposition']
        assert '\r' not in disposition and '\n' not in disposition and '.exe' not in disposition and disposition.endswith('.bin')
        assert client.get(content(env.asset, 'preview-content'), headers=headers()).status_code == 415
        assert client.get(content(env.asset, 'preview-manifest'), headers=headers()).json()['state'] == 'not_requested'


def test_opened_stream_revalidates_permission_and_detects_later_changes(env):
    service = FileService(env.router)
    opened = service.open(env.asset.asset_id, env.asset.sha256, principal=env.owner)
    try:
        assert opened.read(1) == b's'
        path = env.objects / f'objects/{env.asset.sha256[:2]}/{env.asset.sha256}'
        path.write_bytes(b'changed same object')
        with pytest.raises(FileError):
            opened.read(1)
    finally:
        opened.close()
    assert opened.stream.closed


def test_stream_handle_closed_when_transport_disconnects_before_body(env):
    opened = FileService(env.router).open(env.asset.asset_id, env.asset.sha256, principal=env.owner)
    scope = {'type': 'http', 'method': 'GET', 'path': '/', 'headers': [], 'asgi': {'spec_version': '2.4'}}
    response = transfer(opened, Request(scope))
    async def receive(): return {'type': 'http.disconnect'}
    async def send(message): raise OSError('synthetic client disconnected')
    async def run():
        try:
            await response(scope, receive, send)
        except Exception:
            pass
    anyio.run(run)
    assert opened.closed and opened.stream.closed


def test_empty_files_and_backend_mapping_change_preserve_fixed_identity(env):
    from media_tag.storage.filesystem import FilesystemBackend
    (env.originals/'empty.bin').write_bytes(b'')
    empty = env.catalog.register_file(FilesystemBackend(env.originals), 'empty.bin', source_backend_id='synthetic', principal=env.owner, request_id='empty', name='empty')
    with TestClient(app(env)) as client:
        assert client.get(content(empty), headers=headers()).content == b''
        assert client.get(content(empty), headers=headers(Range='bytes=0-')).status_code == 416
        manifest = client.get(content(env.asset, 'download-manifest'), headers=headers()).json()
        # Relocate through the existing backend interface; URL/id/hash are not storage paths.
        env.catalog.backends['alternate'] = env.catalog.object_store
        with env.database.transaction(write=True) as connection:
            connection.execute("UPDATE storage_locations SET backend_id='alternate' WHERE blob_sha256=?", (env.asset.sha256,))
        assert client.get(manifest['content'], headers=headers()).content == b'synthetic source bytes'
