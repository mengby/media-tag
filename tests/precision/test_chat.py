import base64
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from threading import Thread

import pytest

from media_tag.analysis.repair import RepairSubmission, repair
from media_tag.catalog import Principal
from media_tag.models.contracts import ModelFailure
from media_tag.precision.runtime import PrecisionHandlers
from media_tag.router.models import StepSpec
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.precision.chat import analyze
from media_tag.precision.contracts import ChatProvider, PrecisionDeployment
from .test_runtime import result_file

KEY = 'synthetic-precision-test-key'


@contextmanager
def endpoint(reply=None, status=200):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path, self.headers['Authorization'], body))
            context = json.loads(body['messages'][1]['content'][0]['text'])
            content = {'request_id': context['request_id'], 'fields': [
                {'field': name, 'value': {'state': 'unknown', 'reason': 'synthetic observation'}}
                for name in context['fields']]}
            payload = json.dumps({'choices': [{'finish_reason': 'stop', 'message': {'content':
                json.dumps(content if reply is None else reply)}}], 'usage': {'prompt_tokens': 20}}).encode()
            self.send_response(status)
            if status == 307:
                self.send_header('Location', '/must-not-follow')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}/v1', calls
        finally:
            server.shutdown()
            thread.join()


def provider(url, **kwargs):
    return ChatProvider(provider_id='external-test', execution='chat_completions',
        model_id='synthetic-model', base_url=url, inputs=('image', 'audio', 'video'), **kwargs)


def request(path, data, kind):
    path.write_bytes(data)
    return {'input_path': str(path), 'content_kind': kind, 'request_id': 'opaque-test-id',
        'fields': ['画面主体'], 'field_types': {'画面主体': '单选'}, 'published_options': {'画面主体': ['合成']},
        'source_sha256': sha256(data).hexdigest(), 'provider_revision': 'a' * 64,
        'duration_seconds': None, 'evidence_start_seconds': None, 'evidence_end_seconds': None}


@pytest.mark.parametrize('url', ['http://example.org/v1', 'https://user:pw@example.org',
    'https://example.org/?key=secret', 'https://example.org/#frag', 'https://', 'ftp://127.0.0.1'])
def test_invalid_endpoint(url):
    with pytest.raises(ValueError):
        provider(url)


def test_revision_and_local_audio_isolation():
    from media_tag.api.config import LocalConfig
    p = provider('https://example.org/v1')
    assert p.revision == provider(p.base_url, api_key_env='OTHER_TEST_KEY').revision
    assert p.revision != provider('https://different.example/v1').revision
    assert p.revision != p.model_copy(update={'model_id': 'next'}).revision
    with pytest.raises(ValueError, match='external providers belong under precision'):
        LocalConfig.model_validate_json(json.dumps({
            'workspace': 'work', 'database': 'db', 'object_store': 'objects',
            'template_dir': 'templates', 'template_fingerprint': 'a' * 64, 'credentials': {}, 'workers': [],
            'audio_topics': {'providers': [p.model_dump(mode='json')]}}))


@pytest.mark.parametrize(('kind', 'data', 'part_type'), [
    ('audio', b'RIFF0000WAVE' + b'0' * 200000, 'input_audio'),
    ('audio', b'ID3test', 'input_audio'), ('video', b'\x00\x00\x00\x14ftypisom0000mp42', 'video_url')])
def test_complete_encoded_media_and_endpoint(tmp_path, monkeypatch, kind, data, part_type):
    monkeypatch.setenv('MEDIA_TAG_PRECISION_BEARER', KEY)
    req = request(tmp_path/'source.bin', data, kind)
    with endpoint() as (url, calls):
        reply = analyze(provider(url + '/chat/completions'), req, max_input_bytes=len(data),
            timeout_seconds=5, authorize_send=lambda: None)
    (path, auth, body), = calls
    assert (path, auth, body['model']) == ('/v1/chat/completions', 'Bearer ' + KEY, 'synthetic-model')
    part = body['messages'][1]['content'][1]
    assert part['type'] == part_type
    encoded = part[part_type].get('data') or part[part_type]['url'].split(',', 1)[1]
    assert base64.b64decode(encoded, validate=True) == data
    assert reply['usage']['input_tokens'] == 20 and reply['request_id'] == req['request_id']


@pytest.mark.parametrize(('code', 'reply', 'error'), [(401, None, 'auth'), (429, None, 'rate_limit'),
    (307, None, 'http'), (200, {'request_id': 'other', 'fields': []}, 'reply'),
    (200, {'data': 'x' * 530000}, 'reply_budget')])
def test_api_failure(tmp_path, monkeypatch, code, reply, error):
    monkeypatch.setenv('MEDIA_TAG_PRECISION_BEARER', KEY)
    req = request(tmp_path/'source.bin', b'\x89PNG\r\n\x1a\n', 'image')
    with endpoint(reply, code) as (url, calls), pytest.raises(ModelFailure, match='precision_api_' + error):
        analyze(provider(url), req, max_input_bytes=100, timeout_seconds=5, authorize_send=lambda: None)
    assert len(calls) == 1


def test_no_transfer_on_format_budget_or_integrity_failure(tmp_path, monkeypatch):
    monkeypatch.setenv('MEDIA_TAG_PRECISION_BEARER', KEY)
    req = request(tmp_path/'source.bin', b'RIFF0000WAVE', 'audio')
    with endpoint() as (url, calls):
        for changes, limit, code in [({}, 1, 'precision_input_budget'), ({'source_sha256': 'a'*64}, 64,
                'precision_source_integrity'), ({'content_kind': 'video'}, 64, 'precision_api_media_format')]:
            with pytest.raises(ModelFailure, match=code):
                analyze(provider(url), req | changes, max_input_bytes=limit, timeout_seconds=5, authorize_send=lambda: None)
        for brand in (b'qt  ', b'3gp4', b'heic'):
            req = request(tmp_path/'source.bin', b'\x00\x00\x00\x14ftyp' + brand + b'0000isom', 'video')
            with pytest.raises(ModelFailure, match='precision_api_media_format'):
                analyze(provider(url), req, max_input_bytes=64, timeout_seconds=5, authorize_send=lambda: None)
        assert calls == []


@pytest.mark.skipif(os.name != 'nt', reason='precision runner uses Windows Job Objects')
@pytest.mark.parametrize('key_set', [True, False])
def test_external_precision_through_worker(precise, monkeypatch, key_set):
    p = precise
    if key_set:
        monkeypatch.setenv('TEST_REMOTE_PRECISION_KEY', KEY)
    else:
        monkeypatch.delenv('TEST_REMOTE_PRECISION_KEY', raising=False)
    with endpoint() as (url, calls):
        remote = provider(url, api_key_env='TEST_REMOTE_PRECISION_KEY')
        deploy = PrecisionDeployment(enabled=True, providers=(remote,), visual_provider=remote.provider_id)
        p.analysis.precision_deployment = deploy
        _, accepted, executor = p.submit(deploy, fields=('画面主体',))
        assert executor.run_once()['accepted']
        result = result_file(p, accepted['task_id'])
        if not key_set:
            assert result['errors'][0]['code'] == 'precision_api_key_missing'
            assert calls == []
            return
    assert result['status'] == 'succeeded'
    (path, auth, body), = calls
    assert (path, auth) == ('/v1/chat/completions', 'Bearer ' + KEY)
    text_part, media = body['messages'][1]['content']
    context = json.loads(text_part['text'])
    assert context['fields'] == ['画面主体'] and 'input_path' not in context
    actual = base64.b64decode(media['image_url']['url'].split(',', 1)[1], validate=True)
    report_ref = next(a for a in result['artifacts'] if a['kind'] == 'precision_report')
    report_path = p.env.workspace/report_ref['ref']
    report = json.loads(report_path.read_text(encoding='utf8'))
    assert report['document']['content_sha256'] == sha256(actual).hexdigest()
    assert report['reply']['provider_revision'] == remote.revision
    view = p.analysis.status(p.sought.asset.asset_id, principal=p.env.owner)['topic_analysis']
    assert any(f['field'] == '画面主体' and f['origin'] == 'precision' for f in view['fields'])
    assert not view['effective_values_written']
    for name in ('request.json', 'response.json', 'precision.json', 'native.stdout.log', 'native.stderr.log'):
        assert KEY.encode() not in (report_path.parent/name).read_bytes()


@pytest.mark.skipif(os.name != 'nt', reason='precision runner uses Windows Job Objects')
@pytest.mark.parametrize('fields', [(), ('画面主体',)])
def test_generic_router_replan_has_no_transfer_grant(precise, monkeypatch, fields):
    p = precise
    monkeypatch.setenv('MEDIA_TAG_PRECISION_API_KEY', KEY)
    with endpoint() as (url, calls):
        remote = provider(url)
        deploy = PrecisionDeployment(enabled=True, providers=(remote,), visual_provider=remote.provider_id)
        _, granted, executor = p.submit(deploy, fields=('画面主体',))
        with p.env.database.transaction() as db:
            spec = StepSpec.model_validate_json(db.execute('SELECT spec_json FROM router_tasks WHERE task_id=?',
                (granted['task_id'],)).fetchone()[0])
        view = p.env.router.replan(granted['job_id'], (spec.model_copy(update={'target_fields': fields}),),
            expected_revision=granted['accepted_job_revision'], principal=p.env.owner)
        task = next(t for t in view['tasks'] if t['key'] == spec.key)
        assert task['task_id'] != granted['task_id']
        executor.run_once()
        assert result_file(p, task['task_id'])['errors'][0]['code'] == 'precision_transfer_denied'
        assert not calls


@pytest.mark.skipif(os.name != 'nt', reason='precision runner uses Windows Job Objects')
def test_revoke_edit_before_post_keeps_media_and_key_local(precise, tmp_path, monkeypatch):
    from PIL import Image
    p = precise
    owner = Principal(actor_id='synthetic-source-owner')
    source = tmp_path/'precision-sources'/'grant-test.png'
    Image.new('RGB', (17, 11), 'blue').save(source)
    asset = p.env.catalog.register_file(FilesystemBackend(source.parent), source.name, source_backend_id='grant-test',
        principal=owner, request_id='shared-source', name='synthetic')
    readable = p.env.catalog.change_access(asset.asset_id, principal=owner, expected_revision=asset.revision,
        subject_id=p.env.owner.actor_id, permission='read', reason='synthetic shared read')
    shared = p.env.catalog.change_access(asset.asset_id, principal=owner, expected_revision=readable.revision,
        subject_id=p.env.owner.actor_id, permission='edit', reason='synthetic transfer test')
    p.prepare(shared.asset_id)
    authorize = PrecisionHandlers.authorize_transfer
    def revoke_at_handoff(handler, task):
        if (p.env.workspace/task.output_prefix/'transfer.ready').exists():
            current = p.env.catalog.get_asset(shared.asset_id, principal=owner)
            p.env.catalog.change_access(shared.asset_id, principal=owner, expected_revision=current.revision,
                subject_id=p.env.owner.actor_id, permission='edit', revoke=True, reason='synthetic revocation')
        return authorize(handler, task)
    monkeypatch.setattr(PrecisionHandlers, 'authorize_transfer', revoke_at_handoff)
    monkeypatch.setenv('MEDIA_TAG_PRECISION_API_KEY', KEY)
    with endpoint() as (url, calls):
        remote = provider(url)
        deploy = PrecisionDeployment(enabled=True, providers=(remote,), visual_provider=remote.provider_id)
        _, granted, executor = p.submit(deploy, asset_id=shared.asset_id, fields=('画面主体',))
        executor.run_once()
        assert not calls
        with p.env.database.transaction() as db:
            permissions = db.execute('SELECT permission FROM asset_permissions WHERE asset_id=? AND subject_id=?',
                (shared.asset_id, p.env.owner.actor_id)).fetchall()
            assert [row[0] for row in permissions] == ['read']


@pytest.mark.skipif(os.name != 'nt', reason='precision runner uses Windows Job Objects')
def test_authorized_repair_gets_its_own_transfer_grant(precise, monkeypatch):
    p = precise
    monkeypatch.delenv('MEDIA_TAG_PRECISION_API_KEY', raising=False)
    with endpoint() as (url, calls):
        remote = provider(url)
        deploy = PrecisionDeployment(enabled=True, providers=(remote,), visual_provider=remote.provider_id)
        p.analysis.precision_deployment = deploy
        asset = p.sought.asset
        spec, _, executor = p.submit(deploy, fields=('画面主体',))
        executor.run_once()
        assert not calls
        monkeypatch.setenv('MEDIA_TAG_PRECISION_API_KEY', KEY)
        state = p.analysis.status(asset.asset_id, principal=p.env.owner)
        repair(p.analysis, asset.asset_id, RepairSubmission(request_id='repair-remote',
            expected_asset_revision=spec.expected_asset_revision, expected_job_revision=state['runtime']['revision'],
            source_sha256=spec.source_sha256, fields=('画面主体',)), principal=p.env.owner)
        assert executor.run_once()['accepted']
        assert len(calls) == 1
