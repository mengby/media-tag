"""Independent leases for remote images inside the existing supervised worker process."""
from hashlib import sha256
import json


def image_worker_ids(primary, count):
    prefix = 'local-image-' + sha256(primary.encode()).hexdigest()[:24]
    return [f'{prefix}-{number}' for number in range(count)]


def remote_image(row, provider_id):
    if not row['step_key'].startswith('precision-'):
        return False
    spec = json.loads(row['spec_json'])
    return spec['config'].get('content_kind') == 'image' and spec['config'].get('provider_id') == provider_id
