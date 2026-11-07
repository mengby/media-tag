"""Local configuration is private, explicitly selected and never loaded from adjacent projects."""

from pathlib import Path

from media_tag.catalog import Catalog, Database, Principal
from media_tag.contracts.models import ContractModel, Identifier, Sha256
from media_tag.contracts.templates import load_template_bundle
from media_tag.router.models import RouterOptions
from media_tag.router.runtime import CPUHandlers, LocalExecutor, cpu_stages
from media_tag.router.service import Router
from media_tag.storage.filesystem import FilesystemBackend, require_local_path
from media_tag.workers.probe import Toolchain
from media_tag.upload.models import UploadOptions
from media_tag.upload.service import UploadService
from media_tag.variants.service import VariantService
from .app import create_app


class LocalConfig(ContractModel):
    workspace: str
    database: str
    object_store: str
    template_dir: str
    template_fingerprint: Sha256
    credentials: dict[Sha256, Principal]
    workers: tuple[Identifier, ...]
    options: RouterOptions = RouterOptions()
    tool_dir: str | None = None
    upload_options: UploadOptions = UploadOptions()


def load_application(path: Path):
    config = LocalConfig.model_validate_json(path.read_bytes())
    workspace = Path(config.workspace)
    database, objects, templates = Path(config.database), Path(config.object_store), Path(config.template_dir)
    for local in (workspace, database, objects, templates):
        if not local.is_absolute():
            raise ValueError("local configuration paths must be absolute")
        require_local_path(local)
    if not config.credentials or not config.workers:
        raise ValueError("explicit server credentials and workers are required")
    worker_ids = {p.actor_id for p in config.credentials.values() if p.role == "worker"}
    if not set(config.workers) <= worker_ids:
        raise ValueError("each enabled worker requires an explicit credential binding")
    tools = Toolchain(Path(config.tool_dir) if config.tool_dir else None)
    contract = load_template_bundle(templates, expected_fingerprint=config.template_fingerprint)
    catalog = Catalog(Database(database), FilesystemBackend(objects, writable=True), contract=contract)
    router = Router(catalog, workspace, cpu_stages(tools), options=config.options)
    # This capability exists only inside trusted bootstrap, not a caller-controlled role body.
    bootstrap = Principal(actor_id="local-bootstrap", role="admin")
    with router.database.transaction(write=True) as connection:
        connection.execute("UPDATE router_workers SET enabled=0")
        for worker in config.workers:
            connection.execute("""INSERT INTO router_workers VALUES(?,?,1) ON CONFLICT(worker_id)
                DO UPDATE SET stages_json=excluded.stages_json,enabled=1""", (worker, '["probe","evidence"]'))
    router.recover(principal=bootstrap)
    uploads = UploadService(router, tools, options=config.upload_options)
    uploads.dispatch_pending(principal=bootstrap)
    app = create_app(router, config.credentials, uploads=uploads, variants=VariantService(router, tools))
    return app, router, tools


def executor_from_config(path: Path, worker_id: str):
    _, router, tools = load_application(path)
    with router.database.transaction() as connection:
        worker = connection.execute("SELECT enabled FROM router_workers WHERE worker_id=?", (worker_id,)).fetchone()
        if worker is None or not worker[0]:
            raise ValueError("worker must be enabled in the local configuration")
    principal = Principal(actor_id=worker_id, role="worker")
    return LocalExecutor(router, principal, CPUHandlers(router, principal, tools))
