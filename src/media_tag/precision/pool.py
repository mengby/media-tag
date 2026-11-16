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


class RecoveryWindow:
    """Grow after a window of timely replies; older replies do not multiply a backoff."""
    def __init__(self, maximum):
        self.maximum = maximum
        self.limit = min(4, maximum)
        self.epoch = 0
        self.good = 0

    def observe(self, epoch, status, error, seconds, deadline):
        if epoch != self.epoch:
            return
        if error in {'precision_timeout', 'precision_api_connection', 'precision_api_rate_limit'} or seconds > deadline / 2:
            self.limit = max(1, self.limit // 2)
            self.epoch += 1
            self.good = 0
        elif status == 'succeeded' and seconds <= deadline / 4:
            self.good += 1
            if self.good >= self.limit:
                self.limit = min(self.maximum, self.limit + 2)
                self.good = 0


def run_image_workers(workers, stop, *, thread_name='remote-image', recover_timeouts=False):
    """Poll once when idle and dispatch each claimed lease to its owning worker."""
    if not workers:
        return
    idle = deque(workers)
    running = {}
    recovery = RecoveryWindow(len(workers)) if recover_timeouts else None
    with ThreadPoolExecutor(max_workers=len(workers), thread_name_prefix=thread_name) as pool:
        while not stop.is_set():
            for future in tuple(running):
                if future.done():
                    future.result()
                    lane, task, epoch = running.pop(future)
                    idle.append(lane)
                    if recovery:
                        with lane.router.database.transaction() as conn:
                            row = conn.execute("""SELECT json_extract(result_json,'$.status'),
                                json_extract(result_json,'$.errors[0].code'), json_extract(result_json,'$.elapsed_seconds')
                                FROM router_attempts WHERE task_id=? AND lease_epoch=?""",
                                (task.task_id, task.lease_epoch)).fetchone()
                        if row and row[2] is not None:
                            recovery.observe(epoch, *row, task.limits.timeout_seconds)
            if not idle or recovery and len(running) >= recovery.limit:
                wait(running, timeout=.5, return_when=FIRST_COMPLETED)
                continue
            lane = idle[0]
            lease = lane.router.claim(principal=lane.principal)
            if lease is None:
                idle.rotate(-1)
                stop.wait(.5)
            else:
                running[pool.submit(lane.run_lease, lease)] = (idle.popleft(), lease.task, recovery.epoch if recovery else 0)
        for future in running:
            future.result()
