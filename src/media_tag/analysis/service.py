"""Read authorized, requester-scoped intake state without declaring analysis complete."""

from media_tag.catalog import AccessDenied
from media_tag.files.service import FileService

from .contracts import TOPIC_FIELDS
from .intake import INTAKE_POLICY


class AnalysisService:
    def __init__(self, router):
        self.router, self.catalog, self.database = router, router.catalog, router.database
        self.files = FileService(router)

    def status(self, asset_id, *, principal):
        if principal.role not in ("reader", "editor", "admin"):
            raise AccessDenied("human read access required")
        with self.database.transaction() as connection:
            asset = self.catalog._authorize(connection, asset_id, principal)
            row = connection.execute(
                "SELECT * FROM analysis_intents WHERE asset_id=? AND requested_by=? AND policy_version=?",
                (asset_id, principal.actor_id, INTAKE_POLICY),
            ).fetchone()
            gate = self.files._gate(connection, asset)
            if row is None:
                return {"asset_id": asset_id, "state": "not_requested", "analysis_complete": False}
            if row["source_sha256"] != asset["blob_sha256"] or row["owner_id"] != asset["owner_id"]:
                raise ValueError("analysis intent source changed")
            try:
                self.catalog._authorize(connection, asset_id, principal, "edit")
                editable = True
            except AccessDenied:
                editable = False
            state = ("awaiting_file_gate" if gate else "awaiting_edit_permission" if not editable
                     else "awaiting_probe" if row["content_kind"] is None else "pending_analysis")
            return {"asset_id": asset_id, "intent_id": row["intent_id"], "state": state,
                    "content_kind": row["content_kind"], "policy_version": row["policy_version"],
                    "topic_fields": list(TOPIC_FIELDS), "analysis_complete": False,
                    "runtime_dispatched": False, "medical_confirmation": False,
                    "effective_values_written": False}
