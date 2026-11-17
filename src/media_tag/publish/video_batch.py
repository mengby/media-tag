"""Split the live WY registrar from bounded publication, retaining the batch lock and journals."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import json
import time
from threading import Event


class SuccessfulTags:
    def __init__(self, path):
        self.path = path
        self.offset = 0
        self.rows = {}

    def refresh(self):
        with self.path.open("rb") as source:
            source.seek(self.offset)
            for line in source:
                if not line.endswith(b"\n"):
                    break
                row = json.loads(line)
                if row["ok"]:
                    self.rows[row["id"]] = row
                self.offset += len(line)
        return self.rows


def safe_reason(exc):
    message = str(exc)
    return type(exc).__name__ if "://" in message or "Bearer" in message else message[:200]


def registrar(batch, base, record, stop):
    feed = SuccessfulTags(base / "tags-progress.jsonl")
    retry = {}
    while not stop.is_set():
        for digest, row in feed.refresh().items():
            if stop.is_set():
                return
            if digest in batch.reg or time.monotonic() < retry.get(digest, 0):
                continue
            try:
                batch.register(row)
            except Exception as exc:
                retry[digest] = time.monotonic() + 60
                record(base / "wy-errors.jsonl", dict(digest=digest, time=time.time(),
                       stage="registration", error=type(exc).__name__, reason=safe_reason(exc)))
        stop.wait(2)


def follow(batch, base, lines, record, *, trials=(), production_concurrency=1):
    """Feed registered videos to publication without waiting for other uploads."""
    feed = SuccessfulTags(base / "tags-progress.jsonl")
    done = {r["digest"] for r in lines(base / "wy-receipts.jsonl") if r["receipt"]["state"] == "indexed"}
    pending = deque()
    seen = set(done)
    retry = {}
    jobs = {}
    last_refresh = 0

    def attempt(digest, phase, concurrency):
        started = time.time()
        timings = {}
        try:
            asset = batch.reg[digest]
            job = jobs.get(digest) or batch.job(asset)
            if not job:
                return 12
            jobs[digest] = job
            receipt = batch.deliver(feed.rows[digest], asset, job, timings)
            if receipt is None:
                return 12
            status, delay, reason = "indexed", 0, ""
        except Exception as exc:
            reason = safe_reason(exc)
            status, delay = "error", 60
            record(base / "wy-errors.jsonl", dict(digest=digest, time=time.time(), reason=reason,
                   stage=timings.get("failed_stage", "readiness"), error=type(exc).__name__))
        record(base / "wy-stage-timings.jsonl", dict(digest=digest, phase=phase, concurrency=concurrency,
               started=started, time=time.time(), status=status, reason=reason, **timings))
        return delay

    def run_window(capacity, phase, duration, pool, registration):
        nonlocal last_refresh
        started = time.time()
        deadline = time.monotonic() + duration
        active = {}
        while len(done) < len(batch.groups):
            if registration.done():
                registration.result()
                raise RuntimeError("registrar stopped")
            now = time.monotonic()
            if now >= last_refresh + 2:
                rows = feed.refresh()
                for digest in batch.reg.copy():
                    if digest not in seen and digest in rows:
                        pending.append(digest)
                        seen.add(digest)
                last_refresh = now
            if now < deadline:
                for _ in range(len(pending)):
                    if len(active) >= capacity:
                        break
                    digest = pending.popleft()
                    if retry.get(digest, 0) > now:
                        pending.append(digest)
                        continue
                    active[pool.submit(attempt, digest, phase, capacity)] = digest
            if not active:
                if now >= deadline:
                    break
                time.sleep(.25)
                continue
            finished, _ = wait(active, timeout=.5, return_when=FIRST_COMPLETED)
            for future in finished:
                digest = active.pop(future)
                delay = future.result()
                if delay:
                    retry[digest] = time.monotonic() + delay
                    pending.append(digest)
                else:
                    done.add(digest)
                    retry.pop(digest, None)
        record(base / "wy-windows.jsonl", dict(phase=phase, concurrency=capacity, start=started,
                                                end=time.time(), completed=len(done)))

    stop = Event()
    max_workers = max([production_concurrency, *(capacity for _, capacity, _ in trials)])
    with ThreadPoolExecutor(max_workers=1) as reg_pool, ThreadPoolExecutor(max_workers=max_workers) as pub_pool:
        registration = reg_pool.submit(registrar, batch, base, record, stop)
        try:
            existing = {r["phase"] for r in lines(base / "wy-windows.jsonl")}
            for phase, capacity, seconds in trials:
                if phase not in existing and len(done) < len(batch.groups):
                    run_window(capacity, phase, seconds, pub_pool, registration)
            if len(done) < len(batch.groups):
                run_window(production_concurrency, "production", float("inf"), pub_pool, registration)
        finally:
            stop.set()
