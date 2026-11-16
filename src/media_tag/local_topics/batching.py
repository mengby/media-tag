"""Select compatible local topic tasks and prepare verified inputs for one GPU batch."""

from collections import OrderedDict
from hashlib import sha256
import json

from media_tag.router.models import StepSpec
from .service import prepare_image


def cache_key(task_id, epoch, source_sha, spec, parents):
    return (task_id, epoch, source_sha, spec.model_dump_json(),
            tuple((ref.kind, ref.ref, ref.sha256) for ref in parents))


def compatible(first, second):
    return (second.stage == first.stage and second.config == first.config
            and second.target_fields == first.target_fields and second.versions == first.versions
            and second.prompt_version == first.prompt_version and second.limits == first.limits)


class TopicBatch:
    def __init__(self, router, size):
        self.router = router
        self.size = size
        self.cache = OrderedDict()

    def prepare_order(self, rows):
        """Finish the prerequisites of at most one adjacent group before inference."""
        if self.size <= 1 or not rows:
            return rows
        first = rows[0]
        jobs = {}
        for row in rows:
            if (row['job_owner'], row['job_priority']) != (first['job_owner'], first['job_priority']):
                break
            if row['job_id'] not in jobs and len(jobs) == self.size:
                break
            jobs.setdefault(row['job_id'], {})[row['step_key']] = row
        if len(jobs) < 2:
            return rows
        anchor = next((row for row in jobs[first['job_id']].values()
                       if json.loads(row['spec_json'])['stage'] == 'local_topics'), None)
        if anchor is None or any(key[0] == anchor['task_id'] for key in self.cache):
            return rows
        spec = StepSpec.model_validate_json(anchor['spec_json'])
        prerequisites = set()
        for steps in jobs.values():
            member = steps.get(spec.key)
            if member is None or not compatible(spec, StepSpec.model_validate_json(member['spec_json'])):
                continue
            pending = list(json.loads(member['spec_json'])['depends_on'])
            while pending:
                row = steps.get(pending.pop())
                if row is not None and row['task_id'] not in prerequisites:
                    prerequisites.add(row['task_id'])
                    pending.extend(json.loads(row['spec_json'])['depends_on'])
        return sorted(rows, key=lambda row: row['task_id'] not in prerequisites) if prerequisites else rows

    def candidates(self, task, spec, options, folder, check):
        """Read only same-owner inputs already staged by an authorized upstream lease."""
        result = []
        if self.size <= 1:
            return result
        with self.router.database.transaction() as conn:
            anchor = conn.execute('SELECT owner_id,priority FROM router_jobs WHERE job_id=?', (task.job_id,)).fetchone()
            rows = conn.execute("""
                SELECT t.* FROM router_tasks t JOIN router_jobs j USING(job_id)
                WHERE j.owner_id=? AND j.priority=? AND j.cancelled=0 AND j.paused=0
                  AND t.active=1 AND t.state IN ('queued','retry_wait') AND t.ready_at<=?
                  AND t.step_key=? AND json_extract(t.spec_json,'$.stage')='local_topics'
                  AND NOT EXISTS(SELECT 1 FROM router_attempts a WHERE a.task_id=t.task_id AND a.released_at IS NULL)
                ORDER BY j.created_at,t.rowid LIMIT 32
            """, (*anchor, self.router.clock(), spec.key)).fetchall()
            for row in rows:
                check()
                try:
                    step = StepSpec.model_validate_json(row['spec_json'])
                    if not compatible(spec, step):
                        continue
                    job = conn.execute('SELECT * FROM router_jobs WHERE job_id=?', (row['job_id'],)).fetchone()
                    source = self.router._task_source(conn, job, step)
                    ready, refs = self.router._parents(conn, row)
                    if ready != 'ready':
                        continue
                    digest = source['blob_sha256']
                    key = cache_key(row['task_id'], row['lease_epoch'] + 1, digest, step, refs)
                    if key in self.cache:
                        continue
                    source_ref = f"jobs/{job['job_id']}/inputs/objects/{digest[:2]}/{digest}"
                    with self.router.files.open_read(source_ref) as stream:
                        raw = stream.read(options.max_input_bytes + 1)
                    if len(raw) > options.max_input_bytes or sha256(raw).hexdigest() != digest:
                        continue
                    image = prepare_image(raw, options)
                    path = folder / f'candidate-{len(result)}.png'
                    path.write_bytes(image)
                    result.append((key, {'image_path': str(path), 'image_sha256': sha256(image).hexdigest()}))
                    if len(result) == self.size - 1:
                        break
                except Exception:
                    # Its own lease will diagnose an invalid, inaccessible or unavailable candidate.
                    continue
        return result

    def remember(self, key, generation, batch_id, size):
        self.cache[key] = generation, batch_id, size
        while len(self.cache) > self.size:
            self.cache.popitem(last=False)

    def should_yield(self, job_id):
        with self.router.database.transaction() as conn:
            return conn.execute("""
                SELECT 1 FROM router_jobs current JOIN router_jobs j
                  ON j.priority > current.priority OR (j.priority = current.priority AND j.owner_id != current.owner_id)
                JOIN router_tasks t ON t.job_id=j.job_id
                WHERE current.job_id=? AND j.paused=0 AND j.cancelled=0 AND t.active=1
                  AND t.state IN ('queued','retry_wait') AND t.ready_at<=? LIMIT 1
            """, (job_id, self.router.clock())).fetchone() is not None
