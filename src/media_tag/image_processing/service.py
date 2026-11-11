"""Explicit image masking, separate source/output hashes and pending review metadata."""
from hashlib import sha256
import os
from pathlib import Path
import zlib

from PIL import Image, ImageFilter, __version__ as PILLOW_VERSION

from media_tag.contracts.models import Identifier
from media_tag.storage import IntegrityError, UnsafePath
from media_tag.storage.filesystem import FilesystemBackend, require_local_path
from media_tag.transforms.redaction import pixel_box
from media_tag.workers.evidence import _directory

from .animation import decode, encode
from .models import AppliedRegion, FrameResult, ImageRecipe, InspectedFrame, ProcessingLimits, ProcessingResult, SourceInspection, recipe_fingerprint

VERSION = 'ct-image-mask-v1:pillow-'+PILLOW_VERSION+':zlib-'+zlib.ZLIB_RUNTIME_VERSION


class ImageProcessingError(ValueError):
    pass


class ImageProcessor:
    def __init__(self, workspace: Path, *, limits=None):
        workspace = Path(workspace)
        if not workspace.is_absolute():
            raise UnsafePath('absolute existing private workspace required')
        self.files = FilesystemBackend(workspace, writable=True)
        self.workspace = self.files.root
        self.limits = limits or ProcessingLimits()

    def _read_source(self, source, source_key, expected_source_sha256):
        if source.writable:
            raise UnsafePath('source backend must be read-only')
        require_local_path(source.root)
        with source.open_read(source_key) as stream:
            before = os.fstat(stream.fileno())
            if before.st_size > self.limits.max_input_bytes:
                raise ImageProcessingError('input_byte_budget')
            raw = stream.read(self.limits.max_input_bytes+1)
            after = os.fstat(stream.fileno())
        if len(raw) > self.limits.max_input_bytes:
            raise ImageProcessingError('input_byte_budget')
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise IntegrityError('source changed while reading')
        source_hash = sha256(raw).hexdigest()
        if source_hash != expected_source_sha256:
            raise IntegrityError('source hash mismatch')
        return raw, source_hash

    def inspect(self, source: FilesystemBackend, source_key: str, *, expected_source_sha256: str) -> SourceInspection:
        """Read-only canonical frame inspection for explicit declaration hashes; no output files."""
        raw, source_hash = self._read_source(source, source_key, expected_source_sha256)
        try:
            decoded = decode(raw, self.limits)
        except (ValueError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageProcessingError(str(exc)) from None
        frames = []
        for index, frame in enumerate(decoded.frames):
            delay = decoded.delays[index]
            frames.append(InspectedFrame(frame_index=index, role=decoded.roles[index],
                source_rgba_sha256=sha256(frame.tobytes()).hexdigest(), alpha_sha256=sha256(frame.getchannel('A').tobytes()).hexdigest(),
                delay_numerator=delay.numerator if delay else None, delay_denominator=delay.denominator if delay else None))
        return SourceInspection(renderer_version=VERSION, source_sha256=source_hash, source_format=decoded.source_format,
            display_width=decoded.frames[0].width, display_height=decoded.frames[0].height,
            source_exif_orientation=decoded.orientation, frame_count=len(decoded.frames),
            animation_plays=decoded.plays, has_default_poster=decoded.poster, frames=tuple(frames))

    def process(self, source: FilesystemBackend, source_key: str, *, expected_source_sha256: str,
                job_id: str, recipe: ImageRecipe) -> ProcessingResult:
        from pydantic import TypeAdapter
        TypeAdapter(Identifier).validate_python(job_id)
        planned_output = self.workspace/'jobs'/job_id/'image-processing'
        if source.root.is_relative_to(planned_output) or planned_output.is_relative_to(source.root):
            raise UnsafePath('source and private output roots must not overlap')
        raw, source_hash = self._read_source(source, source_key, expected_source_sha256)
        try:
            decoded = decode(raw, self.limits)
            count = len(decoded.frames)
            declarations = {item.frame_index: item for item in recipe.unmasked_frame_declarations}
            for declaration in declarations.values():
                if declaration.frame_index >= count:
                    raise ValueError('unmasked_frame_declaration_outside_input')
                if sha256(decoded.frames[declaration.frame_index].tobytes()).hexdigest() != declaration.source_rgba_sha256:
                    raise ValueError('unmasked_frame_declaration_source_pixels_changed')
                if any(region.purpose == 'face' and region.frame_start <= declaration.frame_index <= (region.frame_end if region.frame_end is not None else count-1)
                        for region in recipe.regions):
                    raise ValueError('unmasked_frame_declaration_conflicts_with_face_region')
            for region in recipe.regions:
                if region.frame_start >= count or region.frame_end is not None and region.frame_end >= count:
                    raise ValueError('region_frame_range_outside_input')
            if any(region.purpose == 'face' for region in recipe.regions):
                for index in range(count):
                    if index not in declarations and not any(region.purpose == 'face' and region.frame_start <= index <= (region.frame_end if region.frame_end is not None else count-1)
                            for region in recipe.regions):
                        raise ValueError('face_regions_must_cover_each_display_frame_including_poster')
            frames, records = [], []
            for index, original in enumerate(decoded.frames):
                rendered = original.copy()
                alpha = original.getchannel('A')
                regions = []
                for region in recipe.regions:
                    if not region.frame_start <= index <= (region.frame_end if region.frame_end is not None else count-1):
                        continue
                    box = pixel_box(region.box, rendered.size, region.padding)
                    x1, y1, x2, y2 = box
                    tile = rendered.crop(box).convert('RGB')
                    if region.method == 'solid':
                        tile = Image.new('RGB', tile.size, region.color)
                    elif region.method == 'blur':
                        tile = tile.filter(ImageFilter.GaussianBlur(radius=region.strength))
                    else:
                        tile = tile.resize((max(1, (x2-x1)//region.strength), max(1, (y2-y1)//region.strength)), Image.Resampling.BOX)
                        tile = tile.resize((x2-x1, y2-y1), Image.Resampling.NEAREST)
                    rendered.paste(tile, box)
                    regions.append(AppliedRegion(region_id=region.region_id, purpose=region.purpose,
                        method=region.method, box_xyxy=box))
                rendered.putalpha(alpha)
                delay = decoded.delays[index]
                frames.append(rendered)
                records.append(FrameResult(frame_index=index, role=decoded.roles[index],
                    delay_numerator=delay.numerator if delay else None, delay_denominator=delay.denominator if delay else None,
                    alpha_sha256=sha256(alpha.tobytes()).hexdigest(), rgba_sha256=sha256(rendered.tobytes()).hexdigest(),
                    source_rgba_sha256=sha256(original.tobytes()).hexdigest(), regions=tuple(regions),
                    unmasked_frame_declaration=declarations.get(index)))
            encoded = encode(tuple(frames), decoded, recipe.output_format, self.limits.max_output_bytes)
            checked = decode(encoded, self.limits)
            if (checked.delays != decoded.delays or checked.roles != decoded.roles or checked.plays != decoded.plays
                    or checked.poster != decoded.poster or checked.icc_profile != decoded.icc_profile or len(checked.frames) != count
                    or any(a.tobytes() != b.tobytes() for a, b in zip(checked.frames, frames))):
                raise ValueError('output_frame_timing_or_pixels_changed')
        except (ValueError, OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageProcessingError(str(exc)) from None
        recipe_hash = recipe_fingerprint(VERSION, recipe)
        folder = _directory(self.workspace, 'jobs/'+job_id+'/image-processing')
        basename = 'image-'+source_hash[:16]+'-'+recipe_hash[:16]
        target, report_path = folder/(basename+'.png'), folder/(basename+'.json')
        if target.exists() or target.is_symlink() or report_path.exists() or report_path.is_symlink():
            raise ImageProcessingError('output_already_exists_requires_existing_result_verification')
        result = ProcessingResult(renderer_version=VERSION, job_id=job_id, source_sha256=source_hash,
            recipe_sha256=recipe_hash, recipe=recipe, output_sha256=sha256(encoded).hexdigest(),
            output_ref=target.relative_to(self.workspace).as_posix(), report_ref=report_path.relative_to(self.workspace).as_posix(),
            source_format=decoded.source_format, output_format=recipe.output_format, display_width=frames[0].width,
            display_height=frames[0].height, source_exif_orientation=decoded.orientation, frame_count=count,
            icc_profile_sha256=sha256(decoded.icc_profile).hexdigest() if decoded.icc_profile is not None else None,
            animation_frame_count=sum(role == 'animation' for role in decoded.roles), animation_plays=decoded.plays,
            has_default_poster=decoded.poster, frames=tuple(records), watermark_quality_status='pending'
                if any(region.purpose == 'watermark' for region in recipe.regions) else 'not_requested')
        with target.open('xb') as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        with self.files.open_read(result.output_ref) as stream:
            if sha256(stream.read()).hexdigest() != result.output_sha256:
                raise IntegrityError('written image output changed')
        with report_path.open('x', encoding='utf8') as stream:
            stream.write(result.model_dump_json(indent=2)); stream.flush(); os.fsync(stream.fileno())
        return result
