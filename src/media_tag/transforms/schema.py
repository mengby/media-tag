"""Migration 4; immutable redaction revisions and output-bound review events."""
REDACTION_SCHEMA = (
    '''CREATE TABLE redaction_revisions(revision_hash TEXT PRIMARY KEY,owner_id TEXT NOT NULL,
       asset_id TEXT NOT NULL REFERENCES assets(asset_id),source_sha256 TEXT NOT NULL,
       scan_task_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,report_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL,
       created_at TEXT NOT NULL,FOREIGN KEY(scan_task_id,lease_epoch) REFERENCES router_attempts(task_id,lease_epoch))''',
    '''CREATE TABLE redaction_runs(run_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
       revision_hash TEXT NOT NULL REFERENCES redaction_revisions(revision_hash),
       plan_id TEXT NOT NULL REFERENCES variant_plans(plan_id),job_id TEXT NOT NULL REFERENCES router_jobs(job_id),
       asset_id TEXT REFERENCES variants(asset_id),UNIQUE(owner_id,request_id))''',
    '''CREATE TABLE privacy_reviews(review_id INTEGER PRIMARY KEY AUTOINCREMENT,asset_id TEXT NOT NULL REFERENCES variants(asset_id),
       output_sha256 TEXT NOT NULL,recipe_hash TEXT NOT NULL,decision TEXT NOT NULL CHECK(decision IN ('approved','revoked')),
       actor_id TEXT NOT NULL,asset_revision INTEGER NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,
       UNIQUE(asset_id,asset_revision))''',
    "CREATE TRIGGER immutable_redaction_revision BEFORE UPDATE ON redaction_revisions BEGIN SELECT RAISE(ABORT,'mask revisions are immutable'); END",
    "CREATE TRIGGER immutable_privacy_review BEFORE UPDATE ON privacy_reviews BEGIN SELECT RAISE(ABORT,'review events are immutable'); END",
)
