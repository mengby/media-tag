"""Migration 5: library enrollment, explicit read grants and durable analysis outbox."""

LIBRARY_SCHEMA = (
    """CREATE TABLE library_entries(asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),
        active INTEGER NOT NULL CHECK(active IN (0,1)), recipients_json TEXT NOT NULL,
        managed_grants_json TEXT NOT NULL, job_id TEXT, dispatch_error TEXT, created_at TEXT NOT NULL)""",
    """CREATE TABLE library_requests(owner_id TEXT NOT NULL,request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL,asset_id TEXT NOT NULL REFERENCES assets(asset_id),
        PRIMARY KEY(owner_id,request_id))""",
    """CREATE TABLE library_events(event_id INTEGER PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES assets(asset_id),
        actor_id TEXT NOT NULL,output_sha256 TEXT NOT NULL,active INTEGER NOT NULL,
        recipients_json TEXT NOT NULL,created_at TEXT NOT NULL)""",
)
