"""Local configuration is private, explicitly selected and never loaded from adjacent projects."""

from pathlib import Path

from pydantic import field_validator

from media_tag.catalog import Catalog, Database, Principal
from media_tag.contracts.models import ContractModel, Identifier, Sha256
from media_tag.analysis.deployment import AnalysisDeployment
from media_tag.analysis.fields import DEFAULT_FIELDS_FILE
from media_tag.precision.contracts import ExternalProvider, PrecisionDeployment
from media_tag.similarity.pipeline import Deployment as SimilarityDeployment
from media_tag.local_topics.contracts import LocalTopicDeployment
from media_tag.processing_queue import ProcessingDeployment, ProcessingHandlers, ProcessingService, stages as processing_stages
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
from media_tag.search.models import SearchOptions
from media_tag.search.service import SearchService
from media_tag.web.local_login import LocalBrowserLogin
from media_tag.semantic.contracts import SemanticDeployment


class ItemPublicationConfig(ContractModel):
    owner_id: Identifier
    base_url: str
    token_file: str
    producer_revision: str


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
    semantic: SemanticDeployment = SemanticDeployment()
    redaction: RedactionDeployment = RedactionDeployment()
    retime: RetimeDeployment = RetimeDeployment()
    web_enabled: bool = False
    local_browser_login: LocalBrowserLogin | None = None
    review: ReviewDeployment = ReviewDeployment(tag_fields_file=str(DEFAULT_FIELDS_FILE))
    terminology: TerminologyDeployment = TerminologyDeployment()
    text: ResolveOptions = ResolveOptions()
    search: SearchOptions = SearchOptions()
    analysis: AnalysisDeployment = AnalysisDeployment()
    precision: PrecisionDeployment = PrecisionDeployment()
    audio_topics: PrecisionDeployment = PrecisionDeployment()
    similarity: SimilarityDeployment = SimilarityDeployment()
    local_topics: LocalTopicDeployment = LocalTopicDeployment()
    processing: ProcessingDeployment = ProcessingDeployment()
    item_publication: ItemPublicationConfig | None = None

    @field_validator('review', mode='before')
    @classmethod
    def default_tag_fields(cls, review):
        if isinstance(review, dict) and 'tag_fields_file' not in review:
            review = {**review, 'tag_fields_file': str(DEFAULT_FIELDS_FILE)}
        if isinstance(review, dict):
            return ReviewDeployment.model_validate_json(json.dumps(review))
        return review

    @field_validator('audio_topics')
    @classmethod
    def local_audio_only(cls, deployment):
        if any(isinstance(provider, ExternalProvider) for provider in deployment.providers):
            raise ValueError('external providers belong under precision only')
        return deployment


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
    if config.processing.enabled:
        router.stages.update(processing_stages(tools))
    if config.similarity.enabled:
        from media_tag.similarity.pipeline import stage as similarity_stage
        router.stages['similarity'] = similarity_stage(tools)
    if config.models.enabled or config.redaction.enabled:
        model_root = Path(config.models.model_root)
        if not model_root.is_absolute():
            raise ValueError('enabled model root must be an absolute local path')
        require_local_path(model_root)
        if config.models.enabled:
            router.stages.update({kind: definition for kind, definition in model_stages(ModelRegistry(model_root), clip_device=config.models.clip_device).items() if kind in config.models.stages})
        if config.redaction.enabled:
            router.stages.update(redaction_stages(ModelRegistry(model_root),tools))
    if config.retime.enabled:
        router.stages.update(retime_stages(tools))
    semantic_registry=None
    router.legacy_image_topics = config.semantic.enabled and not config.local_topics.enabled
    router.upload_configured_fields = router.legacy_image_topics and not config.analysis.enabled
    if config.local_topics.enabled or router.legacy_image_topics:
        if not config.review.enabled:
            raise ValueError('local topic drafts require the template validation policy')
        from media_tag.semantic.registry import SemanticRegistry
        from media_tag.local_topics.runtime import stage as local_topic_stage
        root = Path(config.semantic.model_root if router.legacy_image_topics else config.local_topics.model_root)
        if not root.is_absolute():
            raise ValueError('enabled local topic model root must be absolute')
        require_local_path(root)
        router.watermark_registry = SemanticRegistry(root)
        router.stages['local_topics'] = local_topic_stage(router.watermark_registry)
    if config.precision.enabled:
        if not config.review.enabled:
            raise ValueError('precision drafts require the template validation policy')
        from media_tag.precision.runtime import stage as precision_stage
        router.stages['precision'] = precision_stage()
    if config.audio_topics.enabled:
        if not config.review.enabled:
            raise ValueError('local audio drafts require the template validation policy')
        from media_tag.audio_topics.runtime import stage as audio_topic_stage
        router.stages['audio_topics'] = audio_topic_stage()
    if config.semantic.enabled:
        if not config.review.enabled:raise ValueError('semantic drafts require the template validation policy; no reviewer assignment required')
        from media_tag.semantic.registry import SemanticRegistry
        from media_tag.semantic.runtime import semantic_stage
        root=Path(config.semantic.model_root)
        if not root.is_absolute():raise ValueError('enabled semantic model root must be absolute')
        require_local_path(root)
        semantic_registry=SemanticRegistry(root)
        router.stages['visual_fields']=semantic_stage(semantic_registry)
        router.watermark_registry = semantic_registry
    if getattr(router, 'watermark_registry', None):
        from dataclasses import replace
        from media_tag.cleaning.pretag import STAGE, VERSION, PROMPT_VERSION
        revision = router.watermark_registry.revision
        router.stages[STAGE] = replace(router.stages[STAGE], requires_no_model=False,
            implementation_version=VERSION+':'+PROMPT_VERSION+':'+revision,
            gpu_allowed=True, required_gpu_slots=1, required_model_revision=revision)
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
    reviews=ReviewService(router,config.review) if config.review.enabled else None
    if config.item_publication:
        from media_tag.publish.items import ItemPublisher
        router.item_publisher = ItemPublisher(router,config)
    uploads = UploadService(router, tools, options=config.upload_options)
    uploads.dispatch_pending(principal=bootstrap)
    redactions=RedactionService(router,tools,ModelRegistry(Path(config.models.model_root)),reviewers=config.redaction.reviewers) if config.redaction.enabled else None
    if redactions:
        redactions.adopt_ready()
    retiming=RetimeService(router,tools,reviewers=config.retime.reviewers) if config.retime.enabled else None
    if retiming: retiming.adopt_ready()
    terminology=TerminologyService(router,config.terminology) if config.terminology.enabled else None
    resolver=Resolver(terminology,config.text) if config.text.enabled else None
    if config.search.enabled and resolver is None:raise ValueError('search requires enabled text resolution')
    search=SearchService(resolver,reviews=reviews,options=config.search) if config.search.enabled else None
    if search:
        search.semantic_registry=semantic_registry
        search.library.probe_max_pixels=config.upload_options.probe_max_pixels
    if search and config.models.enabled:
        from media_tag.drafts.visual import VisualEncoder
        search.visual_encoder=VisualEncoder(ModelRegistry(Path(config.models.model_root)),router.workspace)
    if config.local_browser_login is not None and not config.web_enabled:raise ValueError('local browser login requires web UI')
    processing = ProcessingService(router, tools, options=config.processing) if config.processing.enabled else None
    app = create_app(router, config.credentials, uploads=uploads, variants=VariantService(router, tools),redactions=redactions,retiming=retiming,web=config.web_enabled,reviews=reviews,terminology=terminology,resolver=resolver,search=search,local_browser_login=config.local_browser_login,analysis_options=config.analysis,precision_options=config.precision,audio_topics_options=config.audio_topics,processing=processing)
    return app, router, tools


def executor_from_config(path: Path, worker_id: str):
    app, router, tools = load_application(path)
    with router.database.transaction() as connection:
        worker = connection.execute("SELECT enabled FROM router_workers WHERE worker_id=?", (worker_id,)).fetchone()
        if worker is None or not worker[0]:
            raise ValueError("worker must be enabled in the local configuration")
    principal = Principal(actor_id=worker_id, role="worker")
    config = LocalConfig.model_validate_json(path.read_bytes())
    processing_handler = ProcessingHandlers(router, principal, tools, options=config.processing) if config.processing.enabled else None
    similarity_handler = None
    if config.similarity.enabled:
        from media_tag.similarity.pipeline import Handlers as SimilarityHandlers
        similarity_handler = SimilarityHandlers(router, principal, tools)
    models = ModelHandlers(router, principal, ModelRegistry(Path(config.models.model_root)), clip_device=config.models.clip_device)
    semantic_handler=None
    if config.semantic.enabled:
        from media_tag.semantic.registry import SemanticRegistry
        from media_tag.semantic.runtime import SemanticHandlers
        semantic_handler=SemanticHandlers(router,SemanticRegistry(Path(config.semantic.model_root)))
    watermark = None
    if getattr(router, 'watermark_registry', None):
        from media_tag.semantic.runtime import SemanticHandlers
        watermark = (semantic_handler or SemanticHandlers(router,router.watermark_registry)).screen_watermarks
    cpu = CPUHandlers(router, principal, tools, watermark_handler=watermark)
    redactions=RedactionService(router,tools,ModelRegistry(Path(config.models.model_root)),reviewers=config.redaction.reviewers) if config.redaction.enabled else None
    transform=RedactionHandlers(redactions) if redactions else None
    retiming=RetimeService(router,tools,reviewers=config.retime.reviewers) if config.retime.enabled else None
    retime_handler=RedactionHandlers(retiming) if retiming else None
    terms=TerminologyHandlers(app.state.resolver) if config.text.enabled else None
    precision_handler = None
    audio_topic_handler = None
    local_topic_handler = None
    if config.local_topics.enabled or router.legacy_image_topics:
        from media_tag.local_topics.runtime import LocalTopicHandlers
        deployment = (LocalTopicDeployment(enabled=True, model_root=config.semantic.model_root)
                      if router.legacy_image_topics else config.local_topics)
        local_topic_handler = LocalTopicHandlers(router, principal, deployment)
    if config.precision.enabled:
        from media_tag.precision.runtime import PrecisionHandlers
        precision_handler = PrecisionHandlers(router, principal, config.precision)
    if config.audio_topics.enabled:
        from media_tag.audio_topics.runtime import AudioTopicHandlers
        audio_topic_handler = AudioTopicHandlers(router, principal, config.audio_topics)
    latest_asset=[None]
    def handle(lease,stopped):
        latest_asset[0]=lease.task.asset_id
        if lease.task.stage in ('probe','evidence','pretag_cleaning'): return cpu(lease,stopped)
        if lease.task.stage in ('cleaning_output','image_mask_output') and processing_handler:
            return processing_handler(lease, stopped)
        if lease.task.stage == 'similarity' and similarity_handler: return similarity_handler(lease, stopped)
        if lease.task.stage == 'local_topics' and local_topic_handler: return local_topic_handler(lease, stopped)
        if lease.task.stage == 'precision' and precision_handler: return precision_handler(lease, stopped)
        if lease.task.stage == 'audio_topics' and audio_topic_handler: return audio_topic_handler(lease, stopped)
        if lease.task.stage=='visual_fields' and semantic_handler:return semantic_handler(lease,stopped)
        if lease.task.stage in ('face_track','redaction_render') and transform: return transform(lease,stopped)
        if lease.task.stage=='retime_render' and retime_handler: return retime_handler(lease,stopped)
        if lease.task.stage=='terminology_resolve' and terms:return terms(lease,stopped)
        return models(lease,stopped)
    def adopt():
        if processing_handler:
            app.state.processing_derived.adopt_ready(principals=tuple(config.credentials.values()))
        for service in (redactions,retiming):
            if service: service.adopt_ready()
        if config.search.enabled and latest_asset[0]:
            from media_tag.drafts.service import DraftIndex
            with router.database.transaction(write=True) as c:
                asset=c.execute('SELECT * FROM assets WHERE asset_id=? AND deleted_at IS NULL',(latest_asset[0],)).fetchone()
                if asset:DraftIndex(router).sync(c,asset)
    def dispatch_analysis():
        if processing_handler:
            app.state.processing_derived.adopt_ready(principals=tuple(config.credentials.values()))
        if not config.analysis.enabled:
            return
        # Bindings come only from trusted server credentials, never an intent's owner ID.
        seen = set()
        for requester in config.credentials.values():
            if requester.role in ('editor', 'admin') and requester.actor_id not in seen:
                app.state.analysis.dispatch_pending(principal=requester)
                if config.audio_topics.enabled:
                    from media_tag.audio_topics.service import dispatch_pending as dispatch_audio_topics
                    dispatch_audio_topics(app.state.analysis, config.audio_topics, principal=requester)
                if config.precision.enabled:
                    from media_tag.precision.service import dispatch_pending
                    dispatch_pending(app.state.analysis, config.precision, principal=requester)
                seen.add(requester.actor_id)
    return LocalExecutor(router,principal,handle,after_release=adopt,
                         before_acquire=dispatch_analysis if config.analysis.enabled or (config.processing.enabled and config.processing.auto_adopt) else None)
