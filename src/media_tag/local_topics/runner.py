"""Private one-shot runner. Waits for parent ProcessTree assignment before native imports."""
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import time

from media_tag.contracts.templates import read_local_file
from media_tag.models.contracts import ModelFailure
from media_tag.models.lifecycle import watch_parent
from .contracts import LocalTopicOptions
from .native import LocalQwenGenerator
from .prompt import PROMPT_VERSION


def read_image(request):
    path = Path(request['image_path'])
    if path.stat().st_size > 16*1024**2:
        raise ModelFailure('local_topic_model_image_budget')
    raw = read_local_file(path.parent, path.name)
    if sha256(raw).hexdigest() != request['image_sha256']:
        raise ModelFailure('local_topic_model_image_changed')
    from PIL import Image
    with Image.open(BytesIO(raw)) as image:
        if image.format != 'PNG' or image.mode != 'RGB' or image.width*image.height > 2048**2 or getattr(image,'n_frames',1) != 1:
            raise ModelFailure('local_topic_model_image_format')
    return raw


def execute(request):
    if request['prompt_version'] != PROMPT_VERSION:
        raise ModelFailure('local_topic_prompt_changed')
    options = LocalTopicOptions.model_validate_json(json.dumps(request['options']))
    generator = LocalQwenGenerator(request['model_root'], enabled=True)
    if generator.model_revision != request['model_revision'] or generator.model_revision != options.model_revision:
        raise ModelFailure('local_topic_model_changed')
    members = request.get('prefetch', [])
    if len(members) > 31:
        raise ModelFailure('local_topic_batch_budget')
    # Check the leased image first. Optional images are read one GPU microbatch at a time.
    images = [read_image(request), *members]
    results = generator.generate_batch(images=images, field_prompts=tuple(request['field_prompts']), options=options,
        field_rules=request.get('protocol_fields'), microbatch_size=request.get('microbatch_size', 4),
        deadline=request.get('deadline'), read_input=lambda item: item if isinstance(item, bytes) else read_image(item),
        yield_requested=Path(request['start_gate']).with_name('finish-batch').exists)
    # A worker started before batching strictly validates both response objects.
    if 'microbatch_size' not in request and 'prefetch' not in request:
        return {'ok': True, 'generation': results[0].model_dump(mode='json', exclude={'retried_answers'})}
    return {'ok': True, 'generation': results[0].model_dump(mode='json'),
            'prefetched': [result.model_dump(mode='json') for result in results[1:]]}


def main():
    watch_parent(os.getppid())
    request_path, response_path = map(Path, sys.argv[1:3])
    if request_path.stat().st_size > 512*1024:return 2
    request = json.loads(read_local_file(request_path.parent, request_path.name))
    gate = Path(request['start_gate'])
    if gate.parent != request_path.parent or response_path.parent != request_path.parent:return 2
    deadline = time.monotonic()+10.
    while not gate.is_file():
        if time.monotonic() >= deadline:return 2
        time.sleep(.01)
    try:
        response = execute(request)
    except Exception as exc:
        response = {'ok':False, 'error_code':exc.code if isinstance(exc,ModelFailure) else 'local_topic_model_failed'}
    response_path.write_text(json.dumps(response, allow_nan=False), encoding='utf8')
    return 0 if response['ok'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
