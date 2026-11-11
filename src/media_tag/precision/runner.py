"""Private bridge runner. Wait for containment before importing any adapter/model."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys
import time

from media_tag.models.contracts import ModelFailure
from media_tag.contracts.templates import read_local_file

from .contracts import PrecisionProvider, PrecisionReply


def main():
    request_path, response_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding='utf8'))
    deadline = time.monotonic()+10.0
    while not Path(request['start_gate']).is_file():
        if time.monotonic() >= deadline:
            return 2
        time.sleep(.01)
    try:
        provider = PrecisionProvider.model_validate_json(json.dumps(request['provider']))
        path = Path(provider.adapter_path)
        raw = read_local_file(path.parent, path.name)
        if sha256(raw).hexdigest() != provider.adapter_sha256:
            raise ModelFailure('precision_bridge_integrity')
        # Compile the verified bytes instead of re-opening a changed module file.
        module = importlib.util.module_from_spec(importlib.util.spec_from_loader('media_tag_precision_bridge', loader=None))
        module.__file__ = str(path)
        sys.modules[module.__name__] = module
        exec(compile(raw, str(path), 'exec'), module.__dict__)
        # Private input is a generated path; original names, owners and credentials are absent.
        result = module.analyze(request['input'])
        reply = PrecisionReply.model_validate_json(json.dumps(result, allow_nan=False))
        response = {'ok': True, 'reply': reply.model_dump(mode='json')}
    except Exception as exc:
        response = {'ok': False, 'error_code': exc.code if isinstance(exc, ModelFailure) else 'precision_bridge_failed'}
    response_path.write_text(json.dumps(response, allow_nan=False), encoding='utf8')
    return 0 if response['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
