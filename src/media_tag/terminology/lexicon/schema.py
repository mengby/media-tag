"""Append-only migration 7; no changes to migrations 1 through 6."""
TERMINOLOGY_SCHEMA = (
    '''CREATE TABLE terminology_candidates(candidate_id TEXT PRIMARY KEY,kind TEXT NOT NULL,owner_id TEXT NOT NULL,
        base_release_id TEXT,revision INTEGER NOT NULL,state TEXT NOT NULL,payload_json TEXT NOT NULL,
        approvals_json TEXT NOT NULL,created_at TEXT NOT NULL)''',
    '''CREATE TABLE terminology_events(event_id INTEGER PRIMARY KEY,actor_id TEXT NOT NULL,candidate_id TEXT,
        action TEXT NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL)''',
    '''CREATE TABLE terminology_requests(actor_id TEXT NOT NULL,request_id TEXT NOT NULL,fingerprint TEXT NOT NULL,
        result_json TEXT NOT NULL,PRIMARY KEY(actor_id,request_id))''',
    '''CREATE TABLE terminology_releases(release_id TEXT PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,
        candidate_id TEXT NOT NULL REFERENCES terminology_candidates(candidate_id),candidate_revision INTEGER NOT NULL,
        taxonomy_release_id TEXT,payload_json TEXT NOT NULL,bundle_json TEXT NOT NULL,
        published_by TEXT NOT NULL,published_at TEXT NOT NULL,UNIQUE(kind,version))''',
    '''CREATE TABLE terminology_head(singleton INTEGER PRIMARY KEY CHECK(singleton=1),generation INTEGER NOT NULL,
        taxonomy_release_id TEXT REFERENCES terminology_releases(release_id),lexicon_release_id TEXT REFERENCES terminology_releases(release_id))''',
    'INSERT INTO terminology_head VALUES(1,0,NULL,NULL)',
    '''CREATE TRIGGER immutable_terminology_release BEFORE UPDATE ON terminology_releases
        BEGIN SELECT RAISE(ABORT,'terminology release is immutable'); END''',
    '''CREATE TRIGGER undeletable_terminology_release BEFORE DELETE ON terminology_releases
        BEGIN SELECT RAISE(ABORT,'terminology release is immutable'); END''',
    '''CREATE TRIGGER immutable_terminology_event BEFORE UPDATE ON terminology_events
        BEGIN SELECT RAISE(ABORT,'terminology event is immutable'); END''',
)
