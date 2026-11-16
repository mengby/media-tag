from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from PIL import Image, ImageDraw

from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.api.config import LocalConfig
from media_tag.models.contracts import ModelFailure
from media_tag.precision import codex_cli
from media_tag.precision.contracts import CodexProvider, PrecisionDeployment
from .test_chat import request
from .test_runtime import result_file


PROVIDER = CodexProvider(provider_id='codex-terra', execution='codex_cli')


def test_sample_config_selects_only_terra_images(tmp_path):
    example = Path(__file__).resolve().parents[2]/'config'/'precision-codex.example.json'
    settings = json.loads(example.read_bytes())
    deploy = PrecisionDeployment.model_validate_json(json.dumps(settings['precision']))
    assert deploy.enabled and not deploy.auto_dispatch and deploy.visual_provider == PROVIDER.provider_id
    assert deploy.providers == (PROVIDER,)
    configured = PROVIDER.model_dump(mode='json')
    for key, value in [('model_id', 'other'), ('inputs', ['video']), ('cli_path', 'relative/codex')]:
        with pytest.raises(ValueError):
            CodexProvider.model_validate_json(json.dumps(configured | {key: value}))
    with_path = CodexProvider.model_validate_json(json.dumps(configured | {'cli_path': str(tmp_path/'codex.exe')}))
    assert with_path.revision == PROVIDER.revision
    with pytest.raises(ValueError, match='external providers belong under precision'):
        LocalConfig.model_validate_json(json.dumps({
            'workspace': 'work', 'database': 'db', 'object_store': 'objects', 'template_dir': 'templates',
            'template_fingerprint': '0' * 64, 'credentials': {}, 'workers': [], 'audio_topics': settings['precision']}))


def sample():
    image = Image.new('RGB', (240, 160), 'white')
    ImageDraw.Draw(image).ellipse((65, 25, 175, 135), fill='red')
    buffer = BytesIO()
    image.save(buffer, format='PNG')
    return buffer.getvalue()


def test_image_request_and_final_reply(tmp_path, monkeypatch):
    data = sample()
    payload = request(tmp_path/'private-source.bin', data, 'image')
    work = tmp_path/'scratch'
    work.mkdir()
    events = []
    monkeypatch.setattr(codex_cli.shutil, 'which', lambda name: 'codex')

    def run(argv, *, input, cwd, **options):
        events.append('invoke')
        assert argv[argv.index('--model') + 1] == 'gpt-6.1-terra'
        attached = Path(argv[argv.index('--image') + 1])
        assert attached == work/'attachment.png' and attached.read_bytes() == data
        assert argv[-2:] == ['--', '-'] and cwd == work
        prompt = input.decode()
        assert '画面主体' in prompt and 'private-source' not in prompt and str(tmp_path) not in prompt
        schema = json.loads(Path(argv[argv.index('--output-schema') + 1]).read_bytes())
        assert schema['properties']['fields']['items']['properties']['field']['enum'] == ['画面主体']
        final = {'request_id': payload['request_id'], 'fields': [
            {'field': '画面主体', 'value': {'state': 'observed', 'values': ['合成'], 'reason': None},
             'candidates': [], 'dynamic': False}]}
        Path(argv[argv.index('--output-last-message') + 1]).write_text(json.dumps(final), encoding='utf8')
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, 'run', run)
    reply = codex_cli.analyze(PROVIDER, payload, work_dir=work, max_input_bytes=len(data),
        timeout_seconds=10, authorize_send=lambda: events.append('authorized'))
    assert events == ['authorized', 'invoke']
    assert reply['source_sha256'] == sha256(data).hexdigest() and reply['provider_revision'] == PROVIDER.revision


@pytest.mark.parametrize('invalid,code', [('hash', 'precision_source_integrity'),
    ('size', 'precision_input_budget'), ('animation', 'precision_codex_image_format')])
def test_invalid_input_never_launches_codex(tmp_path, monkeypatch, invalid, code):
    data = sample()
    if invalid == 'animation':
        buffer = BytesIO()
        Image.new('RGB', (4, 4), 'red').save(buffer, format='GIF', save_all=True,
            append_images=[Image.new('RGB', (4, 4), 'blue')])
        data = buffer.getvalue()
    payload = request(tmp_path/'source.bin', data, 'image')
    if invalid == 'hash':
        payload['source_sha256'] = '0' * 64
    monkeypatch.setattr(codex_cli.shutil, 'which', lambda name: 'codex')
    with pytest.raises(ModelFailure, match=code):
        codex_cli.analyze(PROVIDER, payload, work_dir=tmp_path, max_input_bytes=1 if invalid == 'size' else len(data),
            timeout_seconds=10, authorize_send=lambda: pytest.fail('invalid attachment authorized'))


@pytest.mark.parametrize('case,code', [('exit', 'precision_codex_process'), ('stale', 'precision_codex_reply'),
    ('malformed', 'precision_codex_reply'), ('oversize', 'precision_codex_reply_budget')])
def test_cli_failure_is_not_a_successful_draft(tmp_path, monkeypatch, case, code):
    payload = request(tmp_path/'source.bin', sample(), 'image')
    monkeypatch.setattr(codex_cli.shutil, 'which', lambda name: 'codex')

    def run(argv, **options):
        content = {'exit': b'{}', 'stale': b'{"request_id":"other","fields":[]}',
            'malformed': b'no JSON', 'oversize': b' ' * (codex_cli.REPLY_LIMIT + 1)}[case]
        Path(argv[argv.index('--output-last-message') + 1]).write_bytes(content)
        return subprocess.CompletedProcess(argv, int(case == 'exit'))

    monkeypatch.setattr(subprocess, 'run', run)
    with pytest.raises(ModelFailure, match=code):
        codex_cli.analyze(PROVIDER, payload, work_dir=tmp_path, max_input_bytes=4096,
            timeout_seconds=10, authorize_send=lambda: None)


@pytest.mark.skipif(os.name != 'nt', reason='precision runner uses Windows Job Objects')
@pytest.mark.parametrize('revoke', [True, False])
def test_codex_worker_preserves_transfer_gate_and_reports_missing_cli(precise, tmp_path, revoke):
    provider = PROVIDER.model_copy(update={'cli_path': str(tmp_path/'absent'/'codex.exe')})
    deployment = PrecisionDeployment(enabled=True, providers=(provider,), visual_provider=provider.provider_id)
    _, accepted, executor = precise.submit(deployment, fields=('画面主体',))
    if revoke:
        with precise.env.database.transaction(write=True) as db:
            db.execute('DELETE FROM router_events WHERE task_id=? AND action=?',
                       (accepted['task_id'], 'precision_request'))
    executor.run_once()
    result = result_file(precise, accepted['task_id'])
    expected = 'precision_transfer_denied' if revoke else 'precision_codex_missing'
    assert result['errors'][0]['code'] == expected
    assert not result['suggestions']


@pytest.mark.skipif(os.getenv('MEDIA_TAG_TEST_LIVE_CODEX') != '1', reason='explicit live synthetic image test')
def test_live_codex_image_through_worker(precise):
    data = sample()
    asset = precise.register(data, 'sample.png', 'image')
    cli = shutil.which('codex.exe' if os.name == 'nt' else 'codex')
    assert cli, 'signed-in native Codex CLI required for the live test'
    provider = PROVIDER.model_copy(update={'cli_path': cli})
    deployment = PrecisionDeployment(enabled=True, providers=(provider,), visual_provider=provider.provider_id)
    precise.analysis.precision_deployment = deployment
    _, accepted, executor = precise.submit(deployment, asset_id=asset.asset_id, fields=TOPIC_FIELDS)
    assert executor.run_once()['accepted']
    result = result_file(precise, accepted['task_id'])
    assert result['status'] == 'succeeded', result['errors']
    artifact = next(a for a in result['artifacts'] if a['kind'] == 'precision_report')
    report = json.loads((precise.env.workspace/artifact['ref']).read_bytes())
    assert report['provider']['model_id'] == 'gpt-6.1-terra'
    assert report['reply']['source_sha256'] == sha256(data).hexdigest()
    fields = report['reply']['fields']
    assert {item['field'] for item in fields} == set(TOPIC_FIELDS)
    observed = ' '.join(value.lower() for item in fields if item['value']['state'] == 'observed'
                        for value in item['value']['values'])
    assert 'red' in observed or '红' in observed
    assert 'circle' in observed or 'disc' in observed or '圆' in observed
    status = precise.analysis.status(asset.asset_id, principal=precise.env.owner)['topic_analysis']
    assert not status['effective_values_written']
