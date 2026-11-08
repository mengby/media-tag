"""Migration 8; serving vocabulary and index switch together, separately from drafts."""
TEXT_INDEX_SCHEMA=(
    '''CREATE TABLE terminology_text_indices(index_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,
        taxonomy_release_id TEXT NOT NULL REFERENCES terminology_releases(release_id),
        lexicon_release_id TEXT NOT NULL REFERENCES terminology_releases(release_id),
        model_revision TEXT NOT NULL,preprocess_version TEXT NOT NULL,bundle_json TEXT NOT NULL,created_at TEXT NOT NULL)''',
    '''CREATE TABLE terminology_serving_head(singleton INTEGER PRIMARY KEY CHECK(singleton=1),epoch INTEGER NOT NULL,
        vocabulary_generation INTEGER NOT NULL,taxonomy_release_id TEXT REFERENCES terminology_releases(release_id),
        lexicon_release_id TEXT REFERENCES terminology_releases(release_id),index_id TEXT REFERENCES terminology_text_indices(index_id))''',
    'INSERT INTO terminology_serving_head VALUES(1,0,0,NULL,NULL,NULL)',
    '''CREATE TRIGGER immutable_text_index BEFORE UPDATE ON terminology_text_indices
        BEGIN SELECT RAISE(ABORT,'text index is immutable'); END''',
    '''CREATE TRIGGER undeletable_text_index BEFORE DELETE ON terminology_text_indices
        BEGIN SELECT RAISE(ABORT,'text index is immutable'); END''',
)
