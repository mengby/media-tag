"""Tag short local videos through an A800 tunnel and save results in a fresh directory."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
from pathlib import Path
import time

from media_tag.workers.probe import Toolchain
from .api import endpoint, infer
from .sampling import prepare, source_still_edge


def write(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    tmp.replace(path)


def tag(item, folder, manifest, prepare_seconds, base_url, model, timeout, started):
    reply = infer(folder, manifest, base_url, model, timeout)
    request_start = reply.pop('request_start') - started
    request_end = reply.pop('request_end') - started
    write(folder / 'tags.json', {'source_sha256': manifest['source_sha256'], **reply})
    entries = manifest['evidence']
    return dict(
        id=item, ok=True, source_sha256=manifest['source_sha256'], prepare_seconds=prepare_seconds,
        request_start=request_start, request_end=request_end, api_seconds=reply['api_seconds'],
        media_bytes=reply['media_bytes'], sent_images=reply['sent_images'], usage=reply['usage'],
        images=sum(e['kind'] == 'image' for e in entries),
        clips=sum(e['kind'] == 'sequence' for e in entries),
    )


def failure(item, stage, exc):
    response = getattr(exc, 'response', None)
    return dict(id=item, ok=False, stage=stage, error=type(exc).__name__,
                status=getattr(response, 'status_code', None), code=getattr(exc, 'code', None),
                detail=str(exc)[:160] if isinstance(exc, ValueError) else None)


def request_peak(rows):
    events = []
    for row in rows:
        if row['ok']:
            events.extend([(row['request_start'], 1), (row['request_end'], -1)])
    active = peak = 0
    for _, delta in sorted(events):
        active += delta
        peak = max(active, peak)
    return peak


def run(files, out, method, concurrency, base_url, model, timeout, tools=None, *,
        staged=False, still_sheet_edge=None, still_edge=None):
    endpoint(base_url)
    if not files or not 1 <= concurrency <= 8:
        raise ValueError('select one or more videos and 1–8 request workers')
    capture_edge = source_still_edge(still_edge, still_sheet_edge)
    files = [Path(f).resolve(strict=True) for f in files]
    if len(set(files)) != len(files):
        raise ValueError('duplicate trial input')
    started = time.perf_counter()
    # Prepare one video at a time; parallelize input decoding within that video.
    tools = tools or Toolchain(decode_threads=min(8, os.cpu_count() or 1))
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=False)
    (out / 'README.md').write_text(
        'Local video tags. Each subdirectory holds the sampled evidence, source clock, and reply.\n',
        encoding='utf-8',
    )
    rows, futures, pending = {}, {}, []
    sampling_options = {} if still_sheet_edge is None else {'still_sheet_edge': still_sheet_edge}
    if still_edge is not None:
        sampling_options['still_edge'] = still_edge
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        def submit(work):
            future = pool.submit(tag, *work, base_url, model, timeout, started)
            futures[future] = work[0]

        for index, path in enumerate(files, 1):
            item = f'v{index:02d}'
            folder = out / item
            t = time.perf_counter()
            try:
                manifest = prepare(path, folder, method, tools, **sampling_options)
            except Exception as exc:
                rows[item] = failure(item, 'prepare', exc)
                continue
            work = (item, folder, manifest, time.perf_counter() - t)
            if staged:
                pending.append(work)
            else:
                submit(work)
        prepare_phase_end = time.perf_counter()
        for work in pending:
            submit(work)
        for future in as_completed(futures):
            item = futures[future]
            try:
                rows[item] = future.result()
            except Exception as exc:
                rows[item] = failure(item, 'inference', exc)
    finished = time.perf_counter()
    wall = finished - started
    results = [rows[k] for k in sorted(rows)]
    count = sum(row['ok'] for row in results)
    report = dict(method=method, model=model, concurrency=concurrency, staged=staged,
                  still_edge=capture_edge, still_sheet_edge=still_sheet_edge,
                  peak_active=request_peak(results), completed=count, expected=len(files),
                  wall_seconds=wall, after_prepare_seconds=finished - prepare_phase_end,
                  videos_per_minute=count * 60 / wall, results=results)
    write(out / 'run.json', report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('videos', nargs='+', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--method', choices=['coverage', 'shots', 'three'], default='coverage',
                        help='frame sampling method (default: coverage)')
    parser.add_argument('--concurrency', type=int, choices=range(1, 9), default=2,
                        help='maximum simultaneous A800 requests (default: 2)')
    parser.add_argument('--staged', action='store_true',
                        help='prepare every video before the first request, for concurrency comparisons')
    layout = parser.add_mutually_exclusive_group()
    layout.add_argument('--still-sheet-edge', type=int, choices=(320, 640),
                        help='send independent stills in one grid, with this maximum long edge per tile')
    layout.add_argument('--still-edge', type=int, choices=(320, 640),
                        help='maximum long edge of each separate still (default: 320)')
    parser.add_argument('--base-url', default='http://127.0.0.1:18080/v1')
    parser.add_argument('--model', default='models')
    parser.add_argument('--timeout', type=float, default=150)
    args = parser.parse_args()
    report = run(args.videos, args.output, args.method, args.concurrency,
                 args.base_url, args.model, args.timeout, staged=args.staged,
                 still_sheet_edge=args.still_sheet_edge, still_edge=args.still_edge)
    print(f"{report['completed']}/{report['expected']}, {report['wall_seconds']:.2f}s, "
          f"{report['videos_per_minute']:.2f} videos/min")
    return 0 if report['completed'] == report['expected'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
