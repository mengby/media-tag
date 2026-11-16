"""Publish completed, cleaning-enrolled originals from a durable per-version queue."""
import json
import logging
import shutil
import tempfile
import time
from contextlib import closing
from hashlib import sha256
from pathlib import Path

from media_contracts import canonical
from media_tag.analysis.contracts import TOPIC_FIELDS
from media_tag.analysis.probe import semantic_content_kind
from media_tag.analysis.projection import TopicProjection
from media_tag.cleaning.pretag import current_screen
from media_tag.files.service import FileService
from media_tag.semantic.batch import _analysis_complete
from .bundle import build_bundle
from .client import PublishSettings, push_bundle


def ready_version(conn, job_id, owner):
    job = conn.execute("""SELECT j.asset_id,a.revision,a.blob_sha256 FROM router_jobs j JOIN assets a USING(asset_id)
        WHERE j.job_id=? AND j.owner_id=? AND a.owner_id=? AND j.cancelled=0 AND a.deleted_at IS NULL
        AND a.kind='original'""", (job_id, owner, owner)).fetchone()
    if not job:
        return None
    tasks = conn.execute("""SELECT t.task_id,t.lease_epoch,t.state,t.spec_json,r.released_at,r.result_json
        FROM router_tasks t LEFT JOIN router_attempts r ON r.task_id=t.task_id AND r.lease_epoch=t.lease_epoch
        WHERE t.job_id=? AND t.active=1 ORDER BY t.task_id""", (job_id,)).fetchall()
    stages = {json.loads(t["spec_json"])["stage"] for t in tasks}
    if "pretag_cleaning" not in stages or not stages.intersection(("local_topics","visual_fields","precision")):
        return None
    if not all(_analysis_complete(t) for t in tasks):
        return None
    signature = [*tuple(job), job_id, [(t["task_id"],t["lease_epoch"]) for t in tasks]]
    return job["asset_id"], sha256(canonical(signature)).hexdigest()


def item_metadata(router, files, conn, asset, detail, principal, item):
    asset_id, version = asset["asset_id"], item["tag_version"]
    if ready_version(conn, item["job_id"], principal.actor_id) != (asset_id, version):
        raise Superseded()
    screen = current_screen(files, asset_id, principal)
    if screen.get("job_id") != item["job_id"] or screen["state"] != "succeeded":
        raise Superseded()
    job = conn.execute("SELECT * FROM router_jobs WHERE job_id=?", (item["job_id"],)).fetchone()
    projection = TopicProjection(router)
    document, _ = projection.build(conn,asset,semantic_content_kind(detail["technical"]),job)
    fields = [{"field":f.field,"state":f.value.state,"values":list(f.value.values or ()),
               "candidates":list(f.candidates),"evidence_ids":list(f.evidence_ids),
               "requires_field_review":f.requires_field_review} for f in document.fields if f.field in TOPIC_FIELDS]
    return {"asset_id":asset_id,"source_sha256":asset["blob_sha256"],"tag_version":version,
            "previous_release":item["previous_release"],"fields":fields,
            "watermark_state":screen["report"]["watermark"]["state"],
            "evidence":{e.evidence_id:e.artifact.sha256 for e in document.evidence},
            "_artifacts":[e.artifact for e in document.evidence]}


class Superseded(Exception):
    """A newer job or revision replaced the version before it was exported."""


class ItemPublisher:
    def __init__(self, router, config):
        self.router = router
        self.config = config
        self.options = config.item_publication
        self.owner = next(p for p in config.credentials.values() if p.actor_id == self.options.owner_id
                          and p.role in ("editor","admin"))
        self.root = Path(config.database).resolve().parent / "item-publications"
        self.client = PublishSettings(base_url=self.options.base_url, token_file=self.options.token_file,
                                      ssh_host=self.options.ssh_host, expected_active_release=None)
        with router.database.transaction(write=True) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS item_deliveries (
                seq INTEGER PRIMARY KEY,asset_id TEXT NOT NULL,tag_version TEXT NOT NULL UNIQUE,
                job_id TEXT NOT NULL,owner_id TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,retry_at REAL NOT NULL DEFAULT 0,
                receipt TEXT,error TEXT)""")
            conn.execute("CREATE INDEX IF NOT EXISTS item_delivery_due ON item_deliveries(owner_id,state,retry_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS item_delivery_asset ON item_deliveries(asset_id,seq)")

    def enqueue(self, conn, job_id):
        provider = self.options.required_precision_provider
        if provider and not conn.execute("""SELECT 1 FROM router_tasks WHERE job_id=? AND active=1
                AND state='succeeded' AND json_extract(spec_json,'$.stage')='precision'
                AND json_extract(spec_json,'$.config.provider_id')=?""", (job_id, provider)).fetchone():
            return
        ready = ready_version(conn, job_id, self.owner.actor_id)
        if ready:
            conn.execute("""INSERT OR IGNORE INTO item_deliveries(asset_id,tag_version,job_id,owner_id)
                VALUES(?,?,?,?)""", (*ready,job_id,self.owner.actor_id))

    def recover(self):
        with self.router.database.transaction() as conn:
            jobs = conn.execute("""SELECT DISTINCT j.job_id FROM router_jobs j JOIN router_tasks t USING(job_id)
                WHERE j.owner_id=? AND j.cancelled=0 AND t.active=1 AND t.step_key='pretag_cleaning'
                ORDER BY j.created_at""",(self.owner.actor_id,)).fetchall()
        for job in jobs:
            with self.router.database.transaction(write=True) as conn:
                self.enqueue(conn,job[0])

    def deliver(self, row, destination):
        asset_id, version = row["asset_id"], row["tag_version"]
        with self.router.database.transaction() as conn:
            previous = conn.execute("""SELECT tag_version FROM item_deliveries WHERE asset_id=?
                AND state='indexed' AND seq<? ORDER BY seq DESC LIMIT 1""",(asset_id,row["seq"])).fetchone()
            asset = self.router.catalog._authorize(conn,asset_id,self.owner)
            source = asset["blob_sha256"]
        files = FileService(self.router)
        files.authorize(asset_id,source,principal=self.owner,for_bytes=True)
        if not destination.exists():
            spec = dict(database=self.config.database, object_store=self.config.object_store,
                        workspace=self.config.workspace, template_dir=self.config.template_dir,
                        review=self.config.review.model_dump(mode="json"),actor_id=self.owner.actor_id,
                        role=self.owner.role,asset_ids=[asset_id],producer_revision=self.options.producer_revision,
                        release_id="item-"+version)
            item = dict(job_id=row["job_id"],tag_version=version,
                        previous_release="item-"+previous[0] if previous else None)
            if self.options.published_owner_id:
                item["published_owner_id"] = self.options.published_owner_id
            with tempfile.TemporaryDirectory(dir=self.root) as scratch:
                bundle = Path(scratch) / "bundle"
                build_bundle(spec,bundle,item=item)
                bundle.rename(destination)
        data = json.loads((destination/"item.json").read_bytes())
        return push_bundle(destination,self.client,item=data,
                           open_original=lambda: closing(files.open(asset_id,source,principal=self.owner)))

    def once(self):
        with self.router.database.transaction() as conn:
            row = conn.execute("""SELECT d.* FROM item_deliveries d WHERE owner_id=? AND state='pending'
                AND retry_at<=? AND NOT EXISTS (SELECT 1 FROM item_deliveries p WHERE p.asset_id=d.asset_id
                AND p.seq<d.seq AND p.state='pending') ORDER BY seq LIMIT 1""",
                (self.owner.actor_id,time.time())).fetchone()
        if row is None:
            return False
        destination = self.root / row["tag_version"]
        try:
            receipt = self.deliver(row,destination)
        except Superseded:
            state, receipt = "superseded", None
        except Exception as exc:
            # Network exceptions may contain a signed URL. Persist only the error class and status.
            error = type(exc).__name__
            if isinstance(exc,ValueError) and str(exc).startswith(("publication HTTP failure ","OSS upload HTTP failure ")):
                error = str(exc)
            delay = min(300,5 * 2 ** min(row["attempts"],6))
            with self.router.database.transaction(write=True) as conn:
                conn.execute("""UPDATE item_deliveries SET attempts=attempts+1,retry_at=?,error=? WHERE seq=?""",
                             (time.time()+delay,error,row["seq"]))
            return True
        else:
            state = "indexed"
        with self.router.database.transaction(write=True) as conn:
            conn.execute("UPDATE item_deliveries SET state=?,receipt=?,error=NULL WHERE seq=?",
                         (state,json.dumps(receipt) if receipt else None,row["seq"]))
        if destination.is_dir() and destination.resolve().parent == self.root:
            shutil.rmtree(destination)
        return True

    def run(self, stop):
        from media_tag.deployment.local import lock
        self.root.mkdir(exist_ok=True)
        (self.root/"README.md").write_text("Private pending single-asset publication bundles.\n",encoding="utf-8")
        with lock(self.root,name="publisher.lock"):
            self.recover()
            while not stop.is_set():
                try:
                    if self.once():
                        continue
                except Exception as exc:
                    logging.getLogger(__name__).error("Item publisher: %s",type(exc).__name__)
                stop.wait(2)
