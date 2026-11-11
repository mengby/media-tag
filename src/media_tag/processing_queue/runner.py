"""Private CPU runner gated until the executor assigns its Windows process tree."""
from hashlib import sha256
import json
from pathlib import Path
import re
import sys
import time

from media_tag.cleaning import CleaningService
from media_tag.image_processing import ImageProcessor
from media_tag.image_processing.service import ImageProcessingError
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.workers.evidence import _directory
from media_tag.workers.probe import Toolchain

from .models import CleaningTask, ImageTask


def execute(request):
    config_type = CleaningTask if request['config']['operation'] == 'cleaning' else ImageTask
    config = config_type.model_validate_json(json.dumps(request['config']))
    source, work = Path(request['source']), Path(request['work'])
    core = _directory(work, 'core')
    if config.operation == 'cleaning':
        result = CleaningService(core, Toolchain(Path(request['tool_dir'])), limits=config.limits).run(
            source, config.recipe, job_id='processing')
    else:
        result = ImageProcessor(core, limits=config.limits).process(FilesystemBackend(source.parent), source.name,
            expected_source_sha256=config.source_sha256, job_id='processing', recipe=config.recipe).model_dump(mode='json')
    return {'state': 'succeeded', 'result': result}


def main():
    request_path, response_path = map(Path, sys.argv[1:3])
    request = json.loads(request_path.read_text(encoding='utf8'))
    deadline = time.monotonic()+10.0
    while not Path(request['start_gate']).is_file():
        if time.monotonic() >= deadline:
            return 2
        time.sleep(.01)
    try:
        response = execute(request)
    except Exception as exc:
        state = getattr(exc, 'state', 'failed')
        image_policies = {
            'face_regions_must_cover_each_display_frame_including_poster',
            'zero_frame_delay_requires_explicit_timing_policy',
            'high_bit_depth_or_color_conversion_requires_explicit_policy',
            'animation_requires_explicit_apng_output',
            'still_input_requires_png_output',
        }
        image_code = str(exc) if isinstance(exc, ImageProcessingError) else None
        if image_code in image_policies:
            state = 'waiting_policy'
        elif image_code == 'missing_positive_frame_timing':
            state = 'waiting_evidence'
        if state not in ('waiting_policy', 'waiting_evidence'):
            state = 'failed'
        code = image_code if image_code in image_policies or image_code == 'missing_positive_frame_timing' else getattr(exc, 'code', 'processing_failed')
        if not isinstance(code, str) or re.fullmatch(r'[a-z][a-z0-9_]{0,127}', code) is None:
            code = 'processing_failed'
        response = {'state': state, 'issue_code': code}
    response_path.write_text(json.dumps(response, allow_nan=False), encoding='utf8')
    return 0 if response['state'] == 'succeeded' else 2


if __name__ == '__main__':
    raise SystemExit(main())
