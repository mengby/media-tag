"""Migration 3; earlier migrations are unchanged."""

UPLOAD_SCHEMA = (
    """CREATE TABLE uploads(
        upload_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        spec_json TEXT NOT NULL, state TEXT NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
        epoch INTEGER NOT NULL DEFAULT 0, lease_until REAL, asset_id TEXT REFERENCES assets(asset_id),
        error_code TEXT, validation_json TEXT, created_at REAL NOT NULL, UNIQUE(owner_id,request_id))""",
    """CREATE TABLE upload_chunks(upload_id TEXT NOT NULL REFERENCES uploads(upload_id), offset INTEGER NOT NULL,
        size INTEGER NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(upload_id,offset))""",
    """CREATE TABLE upload_outbox(event_id TEXT PRIMARY KEY, upload_id TEXT NOT NULL UNIQUE REFERENCES uploads(upload_id),
        asset_id TEXT NOT NULL REFERENCES assets(asset_id), owner_id TEXT NOT NULL, purpose TEXT NOT NULL,
        job_id TEXT, error_code TEXT)""",
    """CREATE TABLE variant_plans(plan_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, parent_asset_id TEXT NOT NULL REFERENCES assets(asset_id),
        source_sha256 TEXT NOT NULL, recipe_hash TEXT NOT NULL, spec_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(owner_id,parent_asset_id,recipe_hash))""",
    """CREATE TABLE variant_requests(owner_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
        plan_id TEXT NOT NULL REFERENCES variant_plans(plan_id), PRIMARY KEY(owner_id,request_id))""",
    """CREATE TABLE variants(asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id), plan_id TEXT NOT NULL UNIQUE REFERENCES variant_plans(plan_id),
        source_sha256 TEXT NOT NULL, output_sha256 TEXT NOT NULL, recipe_hash TEXT NOT NULL, time_map_json TEXT NOT NULL,
        validation_json TEXT NOT NULL, privacy_status TEXT NOT NULL CHECK(privacy_status IN ('pending','not_applicable')),
        task_id TEXT NOT NULL, lease_epoch INTEGER NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(task_id,lease_epoch) REFERENCES router_attempts(task_id,lease_epoch))""",
    """CREATE TABLE variant_source_links(asset_id TEXT NOT NULL REFERENCES variants(asset_id),
        source_id TEXT NOT NULL REFERENCES asset_sources(source_id), PRIMARY KEY(asset_id,source_id))""",
    "CREATE TRIGGER immutable_variant BEFORE UPDATE ON variants BEGIN SELECT RAISE(ABORT,'variant provenance is immutable'); END",
    "CREATE TRIGGER immutable_variant_plan BEFORE UPDATE ON variant_plans BEGIN SELECT RAISE(ABORT,'variant plan is immutable'); END",
)
