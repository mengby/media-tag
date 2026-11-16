"""Collect ready, compatible inputs under the router's ownership and lease rules."""

from collections import OrderedDict
import json

from media_tag.router.models import StepSpec


def cache_key(task_id, epoch, source_sha, spec, parents):
    return (task_id, epoch, source_sha, spec.model_dump_json(),
            tuple((ref.kind, ref.ref, ref.sha256) for ref in parents))


def compatible(first, second):
    return (second.stage == first.stage and second.config == first.config
            and second.target_fields == first.target_fields and second.versions == first.versions
            and second.prompt_version == first.prompt_version and second.limits == first.limits)


class StageBatch:
    def __init__(self, router, size, stage):
        self.router = router
        self.size = size
        self.stage = stage
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
                       if json.loads(row['spec_json'])['stage'] == self.stage), None)
        if anchor is None or any(key[0] == anchor['task_id'] for key in self.cache):
            return rows
        spec = StepSpec.model_validate_json(anchor['spec_json'])
        if not spec.limits.gpu_slots:
            return rows
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

    def collect(self, task, spec, read_item, check):
        """Each item is read from an authorized source and verified, completed parents."""
        result = []
        if self.size <= 1:
            return result
        with self.router.database.transaction() as conn:
            anchor = conn.execute('SELECT owner_id,priority FROM router_jobs WHERE job_id=?', (task.job_id,)).fetchone()
            rows = conn.execute("""
                SELECT t.* FROM router_tasks t JOIN router_jobs j USING(job_id)
                WHERE j.owner_id=? AND j.priority=? AND j.cancelled=0 AND j.paused=0
                  AND t.active=1 AND t.state IN ('queued','retry_wait') AND t.ready_at<=?
                  AND t.step_key=? AND json_extract(t.spec_json,'$.stage')=?
                  AND NOT EXISTS(SELECT 1 FROM router_attempts a WHERE a.task_id=t.task_id AND a.released_at IS NULL)
                ORDER BY j.created_at,t.rowid LIMIT 32
            """, (*anchor, self.router.clock(), spec.key, self.stage)).fetchall()
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
                    item = read_item(row, source, refs, len(result))
                    if item is None:
                        continue
                    result.append((key, item))
                    if len(result) == self.size - 1:
                        break
                except Exception:
                    # Its own lease will diagnose an invalid, inaccessible or unavailable candidate.
                    continue
        return result

    def remember(self, key, value):
        self.cache[key] = value
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
