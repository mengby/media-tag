"""Transaction-bound analysis intent registration; does not run models or grant access."""

from hashlib import sha256

from media_tag.catalog.database import utc_now
from media_tag.storage import IntegrityError

from .probe import KINDS, is_single_frame_gif, semantic_content_kind

INTAKE_POLICY = "ct-all-media-intake-v1"


def ensure_intent(connection, asset_id, requested_by, *, content_kind=None, probe_info=None):
    """Trusted caller must already authorize the registration/upload/derived result."""
    asset = connection.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()
    if asset is None or asset["deleted_at"] is not None:
        raise ValueError("current registered asset required for analysis")
    kind = KINDS.get(content_kind)
    if probe_info is not None:
        normalized=semantic_content_kind(probe_info)
        if content_kind is not None and probe_info.get('kind')!=content_kind:
            raise IntegrityError('analysis technical kind differs from verified probe')
        kind=normalized
    if content_kind is not None and kind is None:
        raise ValueError("unsupported technical content kind")
    key = f"{asset_id}:{requested_by}:{INTAKE_POLICY}"
    identifier = "analysis-" + sha256(key.encode()).hexdigest()
    old = connection.execute("SELECT * FROM analysis_intents WHERE intent_id=?", (identifier,)).fetchone()
    if old:
        if old["source_sha256"] != asset["blob_sha256"] or old["owner_id"] != asset["owner_id"]:
            raise IntegrityError("analysis intent source binding changed")
        if kind and old["content_kind"] and old["content_kind"] != kind:
            if old['content_kind']=='animation' and kind=='image' and is_single_frame_gif(probe_info):
                bound=connection.execute('SELECT 1 FROM router_requests WHERE owner_id=? AND request_id=?',(requested_by,identifier)).fetchone()
                if not bound:
                    connection.execute('UPDATE analysis_intents SET content_kind=? WHERE intent_id=?',(kind,identifier))
                return identifier  # Never silently reinterpret an already bound animation plan.
            raise IntegrityError("analysis intent technical kind changed")
        if kind and old["content_kind"] is None:
            connection.execute("UPDATE analysis_intents SET content_kind=? WHERE intent_id=?", (kind, identifier))
        return identifier
    connection.execute("INSERT INTO analysis_intents VALUES(?,?,?,?,?,?,?,?)", (
        identifier, asset_id, requested_by, asset["owner_id"], asset["blob_sha256"], kind,
        INTAKE_POLICY, utc_now(),
    ))
    return identifier
