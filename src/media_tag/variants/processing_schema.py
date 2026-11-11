"""Append-only migration 12; import-safe before catalogue services initialize."""
PROCESSING_SCHEMA = (
    '''CREATE TABLE processing_derivations(
        asset_id TEXT PRIMARY KEY REFERENCES variants(asset_id), contract_version TEXT NOT NULL,
        quality_required INTEGER NOT NULL CHECK(quality_required=1),
        watermark_required INTEGER NOT NULL CHECK(watermark_required IN (0,1)),
        contract_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)''',
    '''CREATE TABLE processing_output_reviews(
        review_id INTEGER PRIMARY KEY AUTOINCREMENT, asset_id TEXT NOT NULL REFERENCES processing_derivations(asset_id),
        dimension TEXT NOT NULL CHECK(dimension IN ('quality','watermark')),
        output_sha256 TEXT NOT NULL, recipe_hash TEXT NOT NULL, contract_version TEXT NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN ('approved','revoked')), actor_id TEXT NOT NULL,
        asset_revision INTEGER NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(asset_id,asset_revision))''',
    'CREATE INDEX processing_reviews_asset ON processing_output_reviews(asset_id,dimension,review_id)',
    '''CREATE TABLE processing_adoption_receipts(
        task_id TEXT NOT NULL,lease_epoch INTEGER NOT NULL,result_digest TEXT NOT NULL,source_revision INTEGER NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('failed','registered')),asset_id TEXT REFERENCES processing_derivations(asset_id),
        error_code TEXT,attempts INTEGER NOT NULL,last_attempt REAL NOT NULL,retry_after REAL NOT NULL,elapsed_seconds REAL NOT NULL,
        PRIMARY KEY(task_id,lease_epoch),FOREIGN KEY(task_id,lease_epoch) REFERENCES router_attempts(task_id,lease_epoch))''',
    "CREATE TRIGGER immutable_processing_derivation BEFORE UPDATE ON processing_derivations BEGIN SELECT RAISE(ABORT,'processing contracts are immutable'); END",
    "CREATE TRIGGER immutable_processing_review BEFORE UPDATE ON processing_output_reviews BEGIN SELECT RAISE(ABORT,'processing reviews are immutable'); END",
    "CREATE TRIGGER undeletable_processing_review BEFORE DELETE ON processing_output_reviews BEGIN SELECT RAISE(ABORT,'processing reviews are immutable'); END",
    "CREATE TRIGGER undeletable_processing_privacy_review BEFORE DELETE ON privacy_reviews WHEN EXISTS(SELECT 1 FROM processing_derivations WHERE asset_id=OLD.asset_id) BEGIN SELECT RAISE(ABORT,'processing privacy reviews are immutable'); END",
)
