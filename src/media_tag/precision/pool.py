"""Independent leases for remote images inside the existing supervised worker process."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from hashlib import sha256
import json


def image_worker_ids(primary, count):
    prefix = 'local-image-' + sha256(primary.encode()).hexdigest()[:24]
    return [f'{prefix}-{number}' for number in range(count)]


MAX_PREPROCESS_WORKERS = 3


def preprocess_worker_ids(primary, count):
    prefix = 'local-preprocess-' + sha256(primary.encode()).hexdigest()[:24]
    return [f'{prefix}-{number}' for number in range(count)]


def remote_image(row, provider_id):
    if not row['step_key'].startswith('precision-'):
        return False
    spec = json.loads(row['spec_json'])
    return spec['config'].get('content_kind') == 'image' and spec['config'].get('provider_id') == provider_id


def run_image_workers(workers, stop, *, thread_name='remote-image'):
    """Poll once when idle and dispatch each claimed lease to its owning worker."""
    if not workers:
        return
    idle = deque(workers)
    running = {}
    with ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix=thread_name) as pool:
        while not stop.is_set():
            for future in tuple(running):
                if future.done():
                    future.result()
                    idle.append(running.pop(future))
            if not idle:
                wait(running, timeout=.5, return_when=FIRST_COMPLETED)
                continue
            lane = idle[0]
            lease = lane.router.claim(principal=lane.principal)
            if lease is None:
                idle.rotate(-1)
                stop.wait(.5)
            else:
                running[pool.submit(lane.run_lease, lease)] = idle.popleft()
        for future in running:
            future.result()
