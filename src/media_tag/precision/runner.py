"""Private bridge runner. Wait for containment before importing any adapter/model."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import time

from media_tag.models.contracts import ModelFailure
from media_tag.contracts.templates import read_local_file

from .contracts import ChatProvider, PrecisionProvider, PrecisionReply


def run_local(provider, bridge_input):
    path = Path(provider.adapter_path)
    raw = read_local_file(path.parent, path.name)
    if sha256(raw).hexdigest() != provider.adapter_sha256:
        raise ModelFailure('precision_bridge_integrity')
    # Compile the verified bytes instead of re-opening a changed module file.
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader('media_tag_precision_bridge', loader=None))
    module.__file__ = str(path)
    sys.modules[module.__name__] = module
    exec(compile(raw, str(path), 'exec'), module.__dict__)
    return module.analyze(bridge_input)


def main():
    request_path, response_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding='utf8'))
    deadline = time.monotonic()+10.0
    while not Path(request['start_gate']).is_file():
        if time.monotonic() >= deadline:
            return 2
        time.sleep(.01)
    try:
        if request['provider'].get('execution') == 'chat_completions':
            from .chat import analyze
            provider = ChatProvider.model_validate_json(json.dumps(request['provider']))
            def authorize_send():
                request_path.with_name('transfer.ready').touch()
                if sys.stdin.buffer.read(1) != b'1':
                    raise ModelFailure('precision_transfer_denied')

            result = analyze(provider, request['input'], max_input_bytes=request['max_input_bytes'],
                timeout_seconds=request['timeout_seconds'], authorize_send=authorize_send)
        else:
            provider = PrecisionProvider.model_validate_json(json.dumps(request['provider']))
            result = run_local(provider, request['input'])
        reply = PrecisionReply.model_validate_json(json.dumps(result, allow_nan=False))
        response = {'ok': True, 'reply': reply.model_dump(mode='json')}
    except Exception as exc:
        response = {'ok': False, 'error_code': exc.code if isinstance(exc, ModelFailure) else 'precision_bridge_failed'}
    response_path.write_text(json.dumps(response, allow_nan=False), encoding='utf8')
    return 0 if response['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
