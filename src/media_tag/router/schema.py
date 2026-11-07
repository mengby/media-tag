"""Additive migration 2; migration 1 remains byte-for-byte unchanged."""

ROUTER_SCHEMA = (
    """CREATE TABLE router_jobs(
        job_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(asset_id),
        revision INTEGER NOT NULL DEFAULT 1, paused INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL, max_total_attempts INTEGER NOT NULL, created_at REAL NOT NULL)""",
    """CREATE TABLE router_requests(
        owner_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        job_id TEXT NOT NULL REFERENCES router_jobs(job_id), PRIMARY KEY(owner_id,request_id))""",
    """CREATE TABLE router_tasks(
        task_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES router_jobs(job_id), step_key TEXT NOT NULL,
        generation INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1, spec_json TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('queued','leased','running','retry_wait','succeeded','failed','needs_attention','cancelled')),
        attempt INTEGER NOT NULL DEFAULT 0, lease_epoch INTEGER NOT NULL DEFAULT 0, lease_owner TEXT, lease_until REAL,
        ready_at REAL NOT NULL, error_code TEXT, reductions INTEGER NOT NULL DEFAULT 0,
        UNIQUE(job_id,step_key,generation))""",
    "CREATE UNIQUE INDEX router_active_step ON router_tasks(job_id,step_key) WHERE active=1",
    """CREATE TABLE router_dependencies(
        task_id TEXT NOT NULL REFERENCES router_tasks(task_id), parent_id TEXT NOT NULL REFERENCES router_tasks(task_id),
        PRIMARY KEY(task_id,parent_id))""",
    """CREATE TABLE router_attempts(
        task_id TEXT NOT NULL REFERENCES router_tasks(task_id), lease_epoch INTEGER NOT NULL,
        attempt INTEGER NOT NULL, worker_id TEXT NOT NULL, envelope_json TEXT NOT NULL, config_json TEXT NOT NULL,
        started_at REAL NOT NULL, released_at REAL, result_json TEXT, result_digest TEXT, adopted_revision INTEGER,
        PRIMARY KEY(task_id,lease_epoch))""",
    """CREATE TABLE router_workers(
        worker_id TEXT PRIMARY KEY, stages_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1)""",
    """CREATE TABLE router_holds(
        resource TEXT PRIMARY KEY, task_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, worker_id TEXT NOT NULL,
        quarantined INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(task_id,lease_epoch) REFERENCES router_attempts(task_id,lease_epoch))""",
    """CREATE TABLE router_control(id INTEGER PRIMARY KEY CHECK(id=1), paused INTEGER NOT NULL DEFAULT 0)""",
    "INSERT INTO router_control(id,paused) VALUES(1,0)",
    """CREATE TABLE router_receipts(
        receipt_id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, worker_id TEXT NOT NULL,
        digest TEXT NOT NULL, accepted INTEGER NOT NULL, reason TEXT NOT NULL, received_at REAL NOT NULL)""",
    """CREATE TABLE router_events(
        event_id INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES router_jobs(job_id), task_id TEXT,
        actor_id TEXT NOT NULL, action TEXT NOT NULL, detail_json TEXT NOT NULL, created_at REAL NOT NULL)""",
    "CREATE INDEX router_ready ON router_tasks(active,state,ready_at)",
)
