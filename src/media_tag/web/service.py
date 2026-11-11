"""Personnel queries and explicit enrollment; no implicit privacy/medical/license approval."""
from hashlib import sha256
import json

from media_tag.catalog import AccessDenied, CatalogError, Principal, RevisionConflict
from media_tag.catalog.database import utc_now
from media_tag.catalog.service import _human, encode
from media_tag.contracts.models import TaskLimits, Versions
from media_tag.files.service import FileService, FileError
from media_tag.router.models import JobSpec, StepSpec
from media_tag.review.policy import index_status


class LibraryService:
    def __init__(self, router, *, people=(),probe_max_pixels=16*1024**2):
        from media_tag.workers.probe import ProbeOptions
        self.probe_max_pixels=ProbeOptions(max_pixels=probe_max_pixels).max_pixels
        self.router, self.catalog, self.database = router, router.catalog, router.database
        self.files = FileService(router)
        self.people = frozenset(people)

    def enroll(self, asset_id, spec, *, principal):
        _human(principal)
        recipients = sorted(set(spec.recipients) - {principal.actor_id}) if spec.active else []
        if not set(recipients) <= self.people:
            raise ValueError('explicit configured human recipients required')
        # Actual backend/hash verification precedes publication; withdrawal must work with missing files.
        if spec.active:
            opened = self.files.open(asset_id, spec.output_sha256, principal=principal)
            opened.close()
        fingerprint = sha256(encode({'asset_id': asset_id, 'spec': spec.model_dump(mode='json')}).encode()).hexdigest()
        with self.database.transaction(write=True) as connection:
            asset = self.catalog._authorize(connection, asset_id, principal)
            if asset['owner_id'] != principal.actor_id:
                raise AccessDenied('only the asset owner can enroll or share it')
            if asset['blob_sha256'] != spec.output_sha256:
                raise RevisionConflict('enrollment binds exact file hash')
            if spec.active and self.files._gate(connection, asset):
                raise RevisionConflict('privacy or producer gate changed')
            previous = connection.execute('SELECT * FROM library_requests WHERE owner_id=? AND request_id=?',
                                          (principal.actor_id, spec.request_id)).fetchone()
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise RevisionConflict('request ID reused for different enrollment')
            else:
                revision = self.catalog._expected(asset, spec.expected_revision)
                old = connection.execute('SELECT * FROM library_entries WHERE asset_id=?', (asset_id,)).fetchone()
                managed = set(json.loads(old['managed_grants_json'])) if old else set()
                for subject in managed - set(recipients):
                    connection.execute("DELETE FROM asset_permissions WHERE asset_id=? AND subject_id=? AND permission='read'", (asset_id, subject))
                managed &= set(recipients)
                for subject in recipients:
                    existed = connection.execute("SELECT 1 FROM asset_permissions WHERE asset_id=? AND subject_id=? AND permission IN ('read','edit')", (asset_id, subject)).fetchone()
                    if not existed:
                        connection.execute("INSERT INTO asset_permissions VALUES(?,?,'read')", (asset_id, subject))
                        managed.add(subject)
                connection.execute('''INSERT INTO library_entries VALUES(?,?,?,?,NULL,NULL,?) ON CONFLICT(asset_id)
                    DO UPDATE SET active=excluded.active,recipients_json=excluded.recipients_json,managed_grants_json=excluded.managed_grants_json''',
                    (asset_id, int(spec.active), encode(recipients), encode(sorted(managed)), utc_now()))
                connection.execute('INSERT INTO library_requests VALUES(?,?,?,?)', (principal.actor_id, spec.request_id, fingerprint, asset_id))
                connection.execute('INSERT INTO library_events(asset_id,actor_id,output_sha256,active,recipients_json,created_at) VALUES(?,?,?,?,?,?)',
                                   (asset_id, principal.actor_id, spec.output_sha256, int(spec.active), encode(recipients), utc_now()))
                connection.execute('UPDATE assets SET revision=? WHERE asset_id=?', (revision, asset_id))
                self.catalog._event(connection, asset_id, revision, principal, 'library_enrollment', {},
                                    {'active': spec.active, 'recipients': recipients, 'output_sha256': spec.output_sha256})
        self.dispatch_pending(principal=principal)
        return self.entry(asset_id, principal=principal)

    def entry(self, asset_id, *, principal):
        self.files.authorize(asset_id, None, principal=principal)
        with self.database.transaction() as connection:
            row = connection.execute('SELECT * FROM library_entries WHERE asset_id=?', (asset_id,)).fetchone()
            asset = self.catalog._authorize(connection, asset_id, principal)
            legacy = connection.execute("SELECT 1 FROM uploads WHERE asset_id=? AND state='completed' AND json_extract(spec_json,'$.purpose')='library' LIMIT 1", (asset_id,)).fetchone()
            return {'asset_id': asset_id, 'revision': asset['revision'], 'active': bool(row['active'] if row else legacy),
                    'owned':asset['owner_id']==principal.actor_id,
                    'recipients': json.loads(row['recipients_json']) if row and asset['owner_id'] == principal.actor_id else [],
                    'job_id': row['job_id'] if row else None, 'dispatch_error': row['dispatch_error'] if row else None,
                    'medical_confirmation': False, 'license_status': 'unverified'}

    def dispatch_pending(self, *, principal):
        _human(principal)
        with self.database.transaction() as connection:
            rows = connection.execute('''SELECT e.*,a.owner_id FROM library_entries e JOIN assets a USING(asset_id)
                WHERE e.active=1 AND e.job_id IS NULL AND a.deleted_at IS NULL AND (? OR a.owner_id=?)''',
                (int(principal.role == 'admin'), principal.actor_id)).fetchall()
            fingerprint = connection.execute("SELECT value FROM catalog_metadata WHERE key='template_fingerprint'").fetchone()[0]
        for row in rows:
            job_id, error = None, None
            try:
                self.files.authorize(row['asset_id'], None, principal=Principal(actor_id=row['owner_id']), for_bytes=True)
                with self.database.transaction() as connection:
                    prior = connection.execute("SELECT job_id FROM upload_outbox WHERE asset_id=? AND purpose='library' AND job_id IS NOT NULL LIMIT 1", (row['asset_id'],)).fetchone()
                if prior:
                    job_id = prior['job_id']
                else:
                    versions = Versions(rules_version=f'sha256:{fingerprint}', preprocess_version='d02-cpu-v1')
                    steps = [StepSpec(key='probe', stage='probe', versions=versions,config={'max_pixels':self.probe_max_pixels}),
                             StepSpec(key='evidence', stage='evidence', depends_on=('probe',), versions=versions)]
                    for kind in ('ocr', 'face', 'clip','visual_fields'):
                        definition = self.router.stages.get(kind)
                        if definition and definition.required_model_revision:
                            steps.append(StepSpec(key=kind, stage=kind, depends_on=('evidence',),target_fields=definition.allowed_target_fields or (),
                                versions=Versions(rules_version=versions.rules_version, preprocess_version=definition.required_preprocess_version,
                                                  model_revision=definition.required_model_revision),
                                limits=TaskLimits(gpu_slots=definition.required_gpu_slots)))
                    job_id = self.router.submit(JobSpec(asset_id=row['asset_id'], steps=tuple(steps)),
                        request_id=f"library-analysis-{row['asset_id']}", principal=Principal(actor_id=row['owner_id']))['job_id']
            except (CatalogError, FileError, ValueError):
                error = 'analysis_dispatch_unavailable'
            with self.database.transaction(write=True) as connection:
                connection.execute('UPDATE library_entries SET job_id=?,dispatch_error=? WHERE asset_id=? AND job_id IS NULL',
                                   (job_id, error, row['asset_id']))

    def assets(self, *, principal, query='', mine=False, workspace=False, after='', limit=30):
        self.files._role(principal)
        policy=getattr(self.catalog,'review_policy',None)
        # Literal name/ID search only: no unpublished terminology or AI candidate expansion.
        with self.database.transaction() as connection:
            rows = connection.execute('''WITH eligible AS (
                SELECT a.* FROM assets a LEFT JOIN library_entries e USING(asset_id)
                WHERE a.deleted_at IS NULL AND (a.owner_id=? OR EXISTS(SELECT 1 FROM asset_permissions p
                    WHERE p.asset_id=a.asset_id AND p.subject_id=? AND p.permission IN ('read','edit')))
                AND (?=0 OR a.owner_id=?) AND (?=1 AND a.owner_id=? OR
                    coalesce(e.active,EXISTS(SELECT 1 FROM uploads u WHERE u.asset_id=a.asset_id AND u.state='completed'
                       AND json_extract(u.spec_json,'$.purpose')='library'))=1)
            ), matching AS (SELECT DISTINCT root_asset_id FROM eligible WHERE root_asset_id>?
                AND (?='' OR instr(lower(name),lower(?))>0 OR instr(asset_id,?)>0) ORDER BY root_asset_id LIMIT ?)
                SELECT eligible.* FROM eligible JOIN matching USING(root_asset_id) ORDER BY root_asset_id,created_at,asset_id''',
                (principal.actor_id, principal.actor_id, int(mine), principal.actor_id, int(workspace), principal.actor_id,
                 after, query, query, query, limit + 1)).fetchall()
            roots = list(dict.fromkeys(row['root_asset_id'] for row in rows))
            groups = {}
            for row in rows:
                if row['root_asset_id'] not in roots[:limit]:
                    continue
                blocked = self.files._gate(connection, row)
                if blocked and row['owner_id'] != principal.actor_id:
                    continue
                status=index_status(connection,row,policy)
                groups.setdefault(row['root_asset_id'], []).append(dict(asset_id=row['asset_id'], name=row['name'],
                    sha256=row['blob_sha256'], kind=row['kind'], revision=row['revision'], mine=row['owner_id'] == principal.actor_id,
                    file_access=blocked or 'authorized', analysis=self._analysis(connection, row['asset_id'],index_state=status),
                    technical=self.files._technical(connection, row), index_status=status))
        items = [{'root_asset_id': root, 'versions': versions} for root, versions in groups.items()]
        return {'items': items, 'next': roots[limit-1] if len(roots)>limit else None,
                'search_kind': 'literal_name_or_id',
                'medical_review':'available' if policy and principal.role!='reader' and principal.actor_id in policy.config.reviewers else 'not_assigned'}

    def _analysis(self, connection, asset_id, *, index_state=None):
        asset=connection.execute('SELECT * FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
        status=index_state or index_status(connection,asset,getattr(self.catalog,'review_policy',None))
        if status in ('confirmed','needs_review'):return status
        tasks = connection.execute('''SELECT t.state,t.spec_json,j.created_at,j.job_id,j.cancelled,j.paused,a.result_json,a.released_at FROM router_tasks t JOIN router_jobs j USING(job_id)
            LEFT JOIN router_attempts a ON a.task_id=t.task_id AND a.lease_epoch=t.lease_epoch
            WHERE j.asset_id=? AND t.active=1
            ORDER BY CASE WHEN json_extract(t.spec_json,'$.stage') IN ('local_topics','audio_topics','precision') THEN 0 ELSE 1 END,
                t.rowid DESC LIMIT 64''', (asset_id,)).fetchall()
        topic_stages=('local_topics','audio_topics','precision')
        relevant = [t for t in tasks if json.loads(t['spec_json'])['stage'] in ('probe','evidence','ocr','face','clip','visual_fields',*topic_stages)]
        topic_jobs=[t for t in relevant if json.loads(t['spec_json'])['stage'] in topic_stages]
        chosen=topic_jobs[0] if topic_jobs else relevant[0] if relevant else None
        latest = [t for t in relevant if t['job_id']==chosen['job_id']] if chosen else []
        def visual_partial(t):
            if json.loads(t['spec_json'])['stage']!='clip' or t['state']!='needs_attention' or not t['result_json']:return False
            result=json.loads(t['result_json'])
            return result.get('status')=='partial' and result.get('errors') and all(e['code']=='unpublished_vocabulary' for e in result['errors'])
        states = ['succeeded' if visual_partial(t) else t['state'] for t in latest]
        if not states: return 'not_requested'
        if chosen['cancelled']:return 'cancelled'
        if chosen['paused']:return 'paused'
        if any(s in ('failed','needs_attention') for s in states): return 'failed'
        if any(s in ('queued','leased','running','retry_wait') for s in states) or any(t['result_json'] and t['released_at'] is None for t in latest): return 'running'
        if topic_jobs:
            # Stage termination is separate from actual ten-field coverage/confirmation.
            # Reuse shared source/report/current-scope verification; do not trust success alone.
            from media_tag.analysis.intake import KINDS
            from media_tag.analysis.projection import TopicProjection
            intent=connection.execute('''SELECT i.content_kind FROM analysis_intents i JOIN router_requests r
                ON r.request_id=i.intent_id AND r.owner_id=i.requested_by WHERE r.job_id=?
                AND i.asset_id=? AND i.source_sha256=? AND i.owner_id=?''',
                (chosen['job_id'],asset_id,asset['blob_sha256'],asset['owner_id'])).fetchone()
            kind=intent['content_kind'] if intent and intent['content_kind'] else KINDS.get(self.files._technical(connection,asset).get('kind'))
            if kind is None:return 'technical_ready'
            job=connection.execute('SELECT * FROM router_jobs WHERE job_id=?',(chosen['job_id'],)).fetchone()
            projection=TopicProjection(self.router)
            document,_=projection.build(connection,asset,kind,job)
            stage_states={**projection.local_topic_states,**projection.audio_topic_states,**projection.precision_states}
            if any(value in ('unavailable','failed') for value in stage_states.values()):return 'failed'
            if any(value.startswith('verified_') for value in stage_states.values()):return 'waiting_review'
            return 'technical_ready'
        if any(visual_partial(t) for t in latest):return 'ai_draft_ready'
        return 'waiting_review' if any(json.loads(t['spec_json'])['stage'] in ('ocr','face','clip') for t in latest) else 'technical_ready'

    def tasks(self, *, principal):
        _human(principal)
        with self.database.transaction() as connection:
            jobs = []
            for job in connection.execute('SELECT * FROM router_jobs WHERE owner_id=? ORDER BY created_at DESC,job_id DESC LIMIT 100', (principal.actor_id,)):
                try:
                    self.router._source(connection, job)
                except CatalogError:
                    continue
                view = self.router._view(connection, job)
                run = connection.execute('SELECT run_id FROM redaction_runs WHERE job_id=?', (job['job_id'],)).fetchone()
                view['run_id'] = run['run_id'] if run else None
                jobs.append(view)
            uploads = []
            for row in connection.execute('SELECT * FROM uploads WHERE owner_id=? ORDER BY created_at DESC LIMIT 100', (principal.actor_id,)):
                spec = json.loads(row['spec_json'])
                uploads.append({k: row[k] for k in ('upload_id','state','offset','asset_id','error_code')} |
                               {'filename': spec['filename'], 'size_bytes': spec['size_bytes'], 'purpose': spec['purpose']})
        return {'jobs': jobs, 'uploads': uploads, 'limit': 100}

    def retry(self, job_id, revision, *, principal):
        _human(principal)
        with self.database.transaction() as connection:
            job = self.router._job_auth(connection, job_id, principal)
            patches = [StepSpec.model_validate_json(row['spec_json']) for row in connection.execute(
                "SELECT * FROM router_tasks WHERE job_id=? AND active=1 AND state IN ('failed','needs_attention')", (job_id,))]
        if not patches:
            raise RevisionConflict('no failed stages to retry')
        return self.router.replan(job_id, tuple(patches), expected_revision=revision, principal=principal)
