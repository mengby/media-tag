"""Additive migration 10. Existing template and migrations remain unchanged."""
DRAFT_SCHEMA=(
    """CREATE TABLE ai_draft_documents(
        asset_id TEXT NOT NULL REFERENCES assets(asset_id),stage TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,basis_sha256 TEXT NOT NULL,payload_json TEXT NOT NULL,
        updated_at TEXT NOT NULL,PRIMARY KEY(asset_id,stage))""",
)
