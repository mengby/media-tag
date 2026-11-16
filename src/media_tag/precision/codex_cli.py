"""Static image precision through a signed-in Codex CLI."""
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess

from PIL import Image

from media_tag.models.contracts import ModelFailure
from .chat import INSTRUCTIONS, REPLY_LIMIT, prompt_context
from .contracts import PrecisionField


ERROR_CODES = frozenset({
    'precision_codex_missing', 'precision_codex_process', 'precision_codex_reply',
    'precision_codex_reply_budget', 'precision_codex_image_format',
    'precision_input_budget', 'precision_source_integrity', 'precision_timeout',
})


def output_schema(request):
    # Codex strict outputs require every property, including nullable values.
    field = PrecisionField.model_json_schema()
    definitions = field.pop('$defs')
    for obj in (field, definitions['FieldValue']):
        obj['required'] = list(obj['properties'])
        for prop in obj['properties'].values():
            prop.pop('default', None)
    field['properties']['field']['enum'] = request['fields']
    return {
        'type': 'object', 'additionalProperties': False,
        'required': ['request_id', 'fields'], '$defs': definitions,
        'properties': {
            'request_id': {'type': 'string', 'enum': [request['request_id']]},
            'fields': {'type': 'array', 'items': field},
        },
    }


def prepare_attachment(request, work_dir, max_input_bytes):
    if request['content_kind'] != 'image':
        raise ModelFailure('precision_codex_image_format')
    # Give the CLI an anonymous filename with the actual format, even when the
    # source is the worker's extensionless evidence. Verify exactly these bytes.
    digest = sha256()
    staged = work_dir/'attachment.bin'
    with Path(request['input_path']).open('rb') as source, staged.open('xb') as target:
        size = 0
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            if size > max_input_bytes:
                raise ModelFailure('precision_input_budget')
            digest.update(chunk)
            target.write(chunk)
    if digest.hexdigest() != request['source_sha256']:
        raise ModelFailure('precision_source_integrity')
    try:
        with Image.open(staged, formats=['PNG', 'JPEG', 'WEBP', 'GIF']) as image:
            if getattr(image, 'is_animated', False):
                raise ModelFailure('precision_codex_image_format')
            suffix = {'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif', 'WEBP': '.webp'}[image.format]
            image.verify()
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ModelFailure('precision_codex_image_format') from exc
    return staged.rename(staged.with_suffix(suffix))


def analyze(provider, request, *, work_dir, max_input_bytes, timeout_seconds, authorize_send):
    executable = shutil.which(provider.cli_path or ('codex.exe' if os.name == 'nt' else 'codex'))
    if not executable:
        raise ModelFailure('precision_codex_missing')
    attachment = prepare_attachment(request, work_dir, max_input_bytes)
    schema = work_dir/'schema.json'
    schema.write_text(json.dumps(output_schema(request), ensure_ascii=False), encoding='utf8')
    output = work_dir/'reply.json'
    prompt = (INSTRUCTIONS + '\nUse only the attached image. Do not use tools. For a still image, '
              'do not infer change over time or a diagnosis. Describe visible details specifically. '
              'For scalar fields, including text and keyword lists, return one string in values. '
              'Separate keywords within that string. Only 多选 and 关联 allow multiple values. '
              'Use null for absent values or reasons, and [] for absent candidates.\n'
              + json.dumps(prompt_context(request), ensure_ascii=False)).encode('utf8')
    command = [executable, 'exec', '--model', provider.model_id, '--ephemeral', '--ignore-user-config',
               '--sandbox', 'read-only', '--skip-git-repo-check', '-c', 'project_doc_max_bytes=0',
               '-c', 'web_search="disabled"', '--disable', 'shell_tool', '--disable', 'apps',
               '--disable', 'hooks', '--disable', 'multi_agent', '--image', str(attachment),
               '--output-schema', str(schema), '--output-last-message', str(output), '--', '-']
    authorize_send()
    try:
        result = subprocess.run(command, cwd=work_dir, input=prompt, timeout=timeout_seconds,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise ModelFailure('precision_timeout') from None
    except OSError:
        raise ModelFailure('precision_codex_process') from None
    if result.returncode:
        raise ModelFailure('precision_codex_process')
    try:
        with output.open('rb') as stream:
            raw = stream.read(REPLY_LIMIT + 1)
        if len(raw) > REPLY_LIMIT:
            raise ModelFailure('precision_codex_reply_budget')
        reply = json.loads(raw)
        if set(reply) != {'request_id', 'fields'} or reply['request_id'] != request['request_id']:
            raise ValueError
        return dict(reply, source_sha256=request['source_sha256'], provider_revision=provider.revision)
    except (OSError, ValueError, TypeError, KeyError):
        raise ModelFailure('precision_codex_reply') from None
