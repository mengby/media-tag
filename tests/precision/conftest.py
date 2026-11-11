from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from search.conftest import sought, env
from upload.conftest import tools

from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.intake import ensure_intent
from media_tag.analysis.service import AnalysisService
from media_tag.precision.contracts import PrecisionDeployment, PrecisionProvider
from media_tag.precision.runtime import PrecisionHandlers, stage
from media_tag.precision.service import PrecisionSubmission, request_precision
from media_tag.router.runtime import CPUHandlers, LocalExecutor, cpu_stages
from media_tag.storage.filesystem import FilesystemBackend


BRIDGE = '''from hashlib import sha256
from pathlib import Path
import time
import subprocess
import sys

def analyze(request):
    data = Path(request['input_path']).read_bytes()
    assert sha256(data).hexdigest() == request['source_sha256']
    assert 'filename' not in request and 'owner' not in request
    mode = MODE
    if mode == 'sleep':
        time.sleep(60)
    if mode == 'childsleep':
        child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(60)'], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        Path(__file__).with_suffix('.pid').write_text(str(child.pid))
        time.sleep(60)
    fields = []
    for name in request['fields']:
        options = request['published_options'][name]
        if mode == 'outside':
            value = {'state': 'observed', 'values': ['合成未发布候选']}
        elif mode == 'unknown':
            value = {'state': 'unknown', 'reason': 'synthetic uncertainty'}
        elif name in ('科室', '器官/系统', '疾病/症状', '过程/状态', '情绪/语气'):
            value = {'state': 'not_applicable', 'reason': 'synthetic nonmedical geometry'}
        else:
            value = {'state': 'observed', 'values': [options[0] if options else 'synthetic keywords' if name == '英文检索词' else '合成精标圆形']}
        fields.append({'field': name, 'value': value})
    result = {'request_id': request['request_id'], 'source_sha256': request['source_sha256'],
        'provider_revision': request['provider_revision'], 'fields': fields}
    if mode == 'forged_approval':
        result['medical_confirmation'] = True
    if mode == 'wrong_source':
        result['source_sha256'] = '0'*64
    return result
'''


@pytest.fixture
def precise(sought, tools, tmp_path):
    s = sought
    e = s.env
    with e.database.transaction(write=True) as connection:
        connection.execute('UPDATE router_jobs SET cancelled=1')
    e.router.stages.update(cpu_stages(tools))
    e.router.stages['precision'] = stage()
    e.router.register_worker(e.worker.actor_id, ('probe', 'evidence', 'precision'), principal=e.admin)
    folder = tmp_path/'bridge'
    folder.mkdir()
    (folder/'README.md').write_text('Synthetic precision protocol bridge, not a real model.\n')
    bridge = folder/'adapter.py'
    def deployment(mode='normal', **options):
        bridge.write_text('MODE = '+repr(mode)+'\n'+BRIDGE, encoding='utf8')
        provider = PrecisionProvider(provider_id='synthetic-bridge', adapter_path=str(bridge),
            adapter_sha256=sha256(bridge.read_bytes()).hexdigest(), model_id='synthetic-not-a-model',
            model_revision='protocol-only-v1', inputs=('image', 'video', 'audio', 'animation'))
        configured = PrecisionDeployment(enabled=True, providers=(provider,), visual_provider=provider.provider_id,
            audio_provider=provider.provider_id, **options)
        analysis.precision_deployment = configured
        return configured
    analysis = AnalysisService(e.router, AnalysisDeployment(enabled=True))
    def prepare(asset_id=s.asset.asset_id, kind='image'):
        with e.database.transaction(write=True) as connection:
            ensure_intent(connection, asset_id, e.owner.actor_id, content_kind=kind)
        analysis.dispatch_pending(principal=e.owner)
        executor = LocalExecutor(e.router, e.worker, CPUHandlers(e.router, e.worker, tools))
        for _ in range(12):
            if executor.run_once() is None:
                break
        return asset_id
    def submit(deploy, *, asset_id=s.asset.asset_id, fields=None, request='synthetic-precision'):
        asset = e.catalog.get_asset(asset_id, principal=e.owner)
        state = analysis.status(asset_id, principal=e.owner)
        spec = PrecisionSubmission(request_id=request, expected_asset_revision=asset.revision,
            source_sha256=asset.sha256, expected_job_revision=state['runtime']['revision'],
            **({'fields': fields} if fields is not None else {}))
        accepted = request_precision(analysis, deploy, asset_id, spec, principal=e.owner)
        executor = LocalExecutor(e.router, e.worker, PrecisionHandlers(e.router, e.worker, deploy))
        return spec, accepted, executor
    sources = tmp_path/'precision-sources'
    sources.mkdir()
    (sources/'README.md').write_text('Synthetic source media; catalogue reads only.\n')
    def register(data, name, kind):
        (sources/name).write_bytes(data)
        asset = e.catalog.register_file(FilesystemBackend(sources), name, source_backend_id='synthetic-precision',
            principal=e.owner, request_id='source-'+name, name=name)
        prepare(asset.asset_id, kind)
        return asset
    prepare()
    return SimpleNamespace(env=e, sought=s, analysis=analysis, deployment=deployment,
        bridge=bridge, prepare=prepare, submit=submit, register=register, tools=tools)
