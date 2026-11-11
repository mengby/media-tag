from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import warnings

from PIL import Image, ImageOps

from media_tag.analysis.contracts import AnalysisDocument, TOPIC_FIELDS
from media_tag.contracts.models import ArtifactRef, Versions
from media_tag.storage.filesystem import FilesystemBackend
from media_tag.contracts.models import Evidence

from .contracts import Generation, LocalTopicResult, Options
from .parser import assess
from .prompt import PROMPT_VERSION, prompts

PREPROCESS_VERSION = 'local-topic-static-exif-alpha-white-v1'


def prepare_image(raw, options):
    """Deterministic source-to-model pixels; shared by production and verification."""
    if len(raw) > options.max_input_bytes:raise ValueError('source_byte_budget')
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        with Image.open(BytesIO(raw)) as original:
            if original.format not in ('PNG', 'JPEG', 'GIF', 'WEBP', 'BMP', 'TIFF'):
                raise ValueError('unsupported_image_format')
            if getattr(original, 'n_frames', 1) != 1 or getattr(original, 'is_animated', False):
                raise ValueError('animated_input_requires_continuous_evidence_pipeline')
            if original.width*original.height > options.max_source_pixels:
                raise ValueError('source_pixel_budget')
            if original.mode not in ('1', 'L', 'LA', 'P', 'RGB', 'RGBA') or original.info.get('icc_profile'):
                raise ValueError('color_conversion_requires_explicit_policy')
            rgba = ImageOps.exif_transpose(original).convert('RGBA')
            image = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
            image.alpha_composite(rgba)
            image = image.convert('RGB')
            image.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
            buffer = BytesIO();image.save(buffer, format='PNG')
    return buffer.getvalue()


class LocalTopicService:
    """Private workspace service. No catalogue, actor, approval or effective-value writes."""
    def __init__(self, workspace, *, policy, generator, options=None, artifact_root=None):
        self.backend = FilesystemBackend(Path(workspace), writable=True)
        self.policy, self.generator, self.options = policy, generator, options or Options()
        self.artifact_root = FilesystemBackend(Path(artifact_root)).root if artifact_root is not None else self.backend.root
        if self.backend.root != self.artifact_root and self.artifact_root not in self.backend.root.parents:
            raise ValueError('artifact_root_must_contain_private_workspace')

    @staticmethod
    def _readme(directory):
        path = directory/'README.md'
        try:
            with path.open('x', encoding='utf8') as stream:
                stream.write('Private local topic draft artifacts. Source originals are never overwritten.\n')
        except FileExistsError:
            pass

    def _store(self, data, kind):
        # Attempt paths are already long on Windows. Keep object names short,
        # but always verify the complete digest/size before any reuse.
        digest = sha256(data).hexdigest()
        key = 'obj-'+digest[:24]
        path = self.backend._resolve(key,allow_missing=True)
        try:
            with path.open('xb') as stream:
                stream.write(data)
        except FileExistsError:
            pass
        with self.backend.open_verified(key,digest,len(data)):
            pass
        return ArtifactRef(kind=kind, ref=path.relative_to(self.artifact_root).as_posix(), sha256=digest)

    def run(self, source, source_key, *, expected_source_sha256, asset_id, asset_revision,
            analysis_id, content_kind='image'):
        if not isinstance(source, FilesystemBackend):
            raise ValueError('local_readonly_source_backend_required')
        if content_kind not in ('image', 'sticker'):
            raise ValueError('static_images_and_static_stickers_only')
        # Separate output root: a caller cannot accidentally put generated objects in source storage.
        if self.backend.root == source.root or source.root in self.backend.root.parents or self.backend.root in source.root.parents:
            raise ValueError('source_and_draft_workspace_must_be_disjoint')
        with source.open_read(source_key) as stream:
            raw = stream.read(self.options.max_input_bytes+1)
        if len(raw) > self.options.max_input_bytes or sha256(raw).hexdigest() != expected_source_sha256:
            raise ValueError('source_hash_or_byte_budget')
        canonical = prepare_image(raw,self.options)
        self._readme(self.backend.root)
        image_ref = self._store(canonical, 'local_topic_model_image')
        field_prompts = prompts(self.policy, self.options)
        generation = self.generator(image_bytes=canonical, field_prompts=field_prompts, options=self.options)
        if not isinstance(generation, Generation):
            raise ValueError('typed_generation_required')
        if (generation.image_sha256 != image_ref.sha256 or generation.prompt_version != PROMPT_VERSION
                or generation.model_revision != self.generator.model_revision):
            raise ValueError('generation_provenance_mismatch')
        versions = Versions(rules_version=self.policy.digest, preprocess_version=PREPROCESS_VERSION,
            taxonomy_version=self.policy.config.taxonomy_version, model_revision=generation.model_revision)
        evidence_id = 'local_topic_image'
        assessments = tuple(assess(answer, field, policy=self.policy, versions=versions, evidence_id=evidence_id)
                            for field, answer in zip(TOPIC_FIELDS, generation.answers, strict=True))
        report = {'schema_version': 'ct-local-topics-raw-v1', 'source_sha256': expected_source_sha256,
                  'input_content_kind': content_kind, 'options': self.options.model_dump(mode='json'),
                  'image': image_ref.model_dump(mode='json'), 'preprocess_version': PREPROCESS_VERSION,
                  'prompt_version': PROMPT_VERSION, 'policy_digest': self.policy.digest,
                  'taxonomy_version': self.policy.config.taxonomy_version,
                  'generation': generation.model_dump(mode='json'), 'field_prompts': field_prompts,
                  'medical_confirmation': False, 'privacy_confirmation': False, 'effective_values_written': False}
        report_bytes = json.dumps(report, ensure_ascii=False, sort_keys=True).encode()
        if len(report_bytes) > 1024*1024:raise ValueError('local_topic_report_budget')
        raw_ref = self._store(report_bytes, 'local_topic_raw_report')
        document = AnalysisDocument(analysis_id=analysis_id, asset_id=asset_id, asset_revision=asset_revision,
            content_sha256=expected_source_sha256, content_kind='image', fields=assessments,
            evidence=(Evidence(evidence_id=evidence_id, kind='image', source_sha256=expected_source_sha256, artifact=image_ref),))
        return LocalTopicResult(document=document, raw_report=raw_ref, image=image_ref,
            prompt_version=PROMPT_VERSION, policy_digest=self.policy.digest,
            model_revision=generation.model_revision, backend=generation.backend)
