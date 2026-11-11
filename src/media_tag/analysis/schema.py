"""Durable analysis requests, not a second execution queue or a sharing table."""

ANALYSIS_SCHEMA = (
    """CREATE TABLE analysis_intents (
        intent_id TEXT PRIMARY KEY,
        asset_id TEXT NOT NULL REFERENCES assets(asset_id),
        requested_by TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        source_sha256 TEXT NOT NULL REFERENCES blobs(sha256),
        content_kind TEXT CHECK(content_kind IN ('image','video','animation','audio')),
        policy_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE(asset_id, requested_by, policy_version)
    )""",
    "CREATE INDEX analysis_intents_asset ON analysis_intents(asset_id)",
)
