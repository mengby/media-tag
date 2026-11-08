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
from media_tag.models.contracts import ModelDeployment
from media_tag.models.registry import ModelRegistry
from media_tag.models.runtime import ModelHandlers, model_stages
import json
from .app import create_app
from media_tag.transforms.models import RedactionDeployment,RetimeDeployment
from media_tag.transforms.runtime import RedactionHandlers, redaction_stages,retime_stages
from media_tag.transforms.service import RedactionService
from media_tag.transforms.retime_service import RetimeService
from media_tag.review.models import ReviewDeployment
from media_tag.review.service import ReviewService
from media_tag.terminology.contracts import Deployment as TerminologyDeployment
from media_tag.terminology.lexicon.service import TerminologyService
from media_tag.terminology.resolve.models import ResolveOptions
from media_tag.terminology.resolve.service import Resolver
from media_tag.terminology.embedding.registry import BGERegistry
from media_tag.workers.terminology import terminology_stages,TerminologyHandlers


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
    models: ModelDeployment = ModelDeployment()
    redaction: RedactionDeployment = RedactionDeployment()
    retime: RetimeDeployment = RetimeDeployment()
    web_enabled: bool = False
    review: ReviewDeployment = ReviewDeployment()
    terminology: TerminologyDeployment = TerminologyDeployment()
    text: ResolveOptions = ResolveOptions()


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
    if config.models.enabled or config.redaction.enabled:
        model_root = Path(config.models.model_root)
        if not model_root.is_absolute():
            raise ValueError('enabled model root must be an absolute local path')
        require_local_path(model_root)
        if config.models.enabled:
            router.stages.update(model_stages(ModelRegistry(model_root), clip_device=config.models.clip_device))
        if config.redaction.enabled:
            router.stages.update(redaction_stages(ModelRegistry(model_root),tools))
    if config.retime.enabled:
        router.stages.update(retime_stages(tools))
    if config.text.enabled:
        if not config.terminology.enabled:raise ValueError('text resolution requires the terminology service')
        if config.text.model_enabled:
            root=Path(config.text.model_root)
            if not root.is_absolute():raise ValueError('enabled text model root must be absolute')
            require_local_path(root)
        router.stages.update(terminology_stages(BGERegistry(config.text.model_root)))
    # This capability exists only inside trusted bootstrap, not a caller-controlled role body.
    bootstrap = Principal(actor_id="local-bootstrap", role="admin")
    with router.database.transaction(write=True) as connection:
        connection.execute("UPDATE router_workers SET enabled=0")
        for worker in config.workers:
            connection.execute("""INSERT INTO router_workers VALUES(?,?,1) ON CONFLICT(worker_id)
                DO UPDATE SET stages_json=excluded.stages_json,enabled=1""", (worker, json.dumps(sorted(router.stages))))
    router.recover(principal=bootstrap)
    uploads = UploadService(router, tools, options=config.upload_options)
    uploads.dispatch_pending(principal=bootstrap)
    redactions=RedactionService(router,tools,ModelRegistry(Path(config.models.model_root)),reviewers=config.redaction.reviewers) if config.redaction.enabled else None
    if redactions:
        redactions.adopt_ready()
    retiming=RetimeService(router,tools,reviewers=config.retime.reviewers) if config.retime.enabled else None
    if retiming: retiming.adopt_ready()
    reviews=ReviewService(router,config.review) if config.review.enabled else None
    terminology=TerminologyService(router,config.terminology) if config.terminology.enabled else None
    resolver=Resolver(terminology,config.text) if config.text.enabled else None
    app = create_app(router, config.credentials, uploads=uploads, variants=VariantService(router, tools),redactions=redactions,retiming=retiming,web=config.web_enabled,reviews=reviews,terminology=terminology,resolver=resolver)
    return app, router, tools


def executor_from_config(path: Path, worker_id: str):
    app, router, tools = load_application(path)
    with router.database.transaction() as connection:
        worker = connection.execute("SELECT enabled FROM router_workers WHERE worker_id=?", (worker_id,)).fetchone()
        if worker is None or not worker[0]:
            raise ValueError("worker must be enabled in the local configuration")
    principal = Principal(actor_id=worker_id, role="worker")
    config = LocalConfig.model_validate_json(path.read_bytes())
    cpu = CPUHandlers(router, principal, tools)
    models = ModelHandlers(router, principal, ModelRegistry(Path(config.models.model_root)), clip_device=config.models.clip_device)
    redactions=RedactionService(router,tools,ModelRegistry(Path(config.models.model_root)),reviewers=config.redaction.reviewers) if config.redaction.enabled else None
    transform=RedactionHandlers(redactions) if redactions else None
    retiming=RetimeService(router,tools,reviewers=config.retime.reviewers) if config.retime.enabled else None
    retime_handler=RedactionHandlers(retiming) if retiming else None
    terms=TerminologyHandlers(app.state.resolver) if config.text.enabled else None
    def handle(lease,stopped):
        if lease.task.stage in ('probe','evidence'): return cpu(lease,stopped)
        if lease.task.stage in ('face_track','redaction_render') and transform: return transform(lease,stopped)
        if lease.task.stage=='retime_render' and retime_handler: return retime_handler(lease,stopped)
        if lease.task.stage=='terminology_resolve' and terms:return terms(lease,stopped)
        return models(lease,stopped)
    def adopt():
        for service in (redactions,retiming):
            if service: service.adopt_ready()
    return LocalExecutor(router,principal,handle,after_release=adopt)
