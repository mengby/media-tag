from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import json

from PIL import Image, ImageCms, ImageDraw
import pytest

from media_tag.image_processing import ImageProcessor, ImageRecipe, ImageProcessingError, MaskRegion, ProcessingLimits, ProcessingResult, UnmaskedFrameDeclaration
from media_tag.image_processing.animation import Decoded, decode, encode
from media_tag.storage import IntegrityError, UnsafePath
from media_tag.storage.filesystem import FilesystemBackend


@pytest.fixture
def work(tmp_path):
    source, output = tmp_path/'source', tmp_path/'output'
    for folder in (source, output):
        folder.mkdir()
        (folder/'README.md').write_text('Synthetic isolated image processing fixture.\n')
    return source, output


def png(image, **kwargs):
    stream = BytesIO()
    image.save(stream, 'PNG', **kwargs)
    return stream.getvalue()


def grid():
    image = Image.new('RGBA', (16, 12))
    image.putdata([(x*13, y*17, (x+y)*7, (x*19+y*11) % 256) for y in range(12) for x in range(16)])
    return image


def region(**kwargs):
    return MaskRegion(region_id='synthetic-face', purpose='face', box=(.25, .25, .75, .75),
        method=kwargs.pop('method', 'mosaic'), **kwargs)


def run(work, raw, recipe, *, name='synthetic.png', job='synthetic-job', limits=None):
    source, output = work
    (source/name).write_bytes(raw)
    service = ImageProcessor(output, limits=limits)
    result = service.process(FilesystemBackend(source), name, expected_source_sha256=sha256(raw).hexdigest(),
        job_id=job, recipe=recipe)
    return result, (output/result.output_ref).read_bytes()


@pytest.mark.parametrize('method', ['solid', 'mosaic', 'blur'])
def test_region_mask_preserves_original_alpha_and_unmasked_rgba_and_pending_reviews(work, method):
    original = grid()
    raw = png(original)
    recipe = ImageRecipe(output_format='png', regions=(region(method=method),))
    result, encoded = run(work, raw, recipe)
    actual = Image.open(BytesIO(encoded)).convert('RGBA')
    assert actual.getchannel('A').tobytes() == original.getchannel('A').tobytes()
    for y in range(original.height):
        for x in range(original.width):
            if not (4 <= x < 12 and 3 <= y < 9):
                assert actual.getpixel((x, y)) == original.getpixel((x, y))
    assert actual.crop((4, 3, 12, 9)).tobytes() != original.crop((4, 3, 12, 9)).tobytes()
    assert (work[0]/'synthetic.png').read_bytes() == raw
    assert result.output_sha256 == sha256(encoded).hexdigest()
    assert result.privacy_status == result.quality_status == 'pending'
    assert not result.face_detection_performed and not result.privacy_confirmation
    assert not result.quality_confirmation and not result.license_confirmation
    assert not result.effective_values_written
    report = json.loads((work[1]/result.report_ref).read_text(encoding='utf8'))
    assert report['output_sha256'] == result.output_sha256


def test_watermark_mask_is_not_restoration_or_privacy_approval(work):
    item = MaskRegion(region_id='watermark-region', purpose='watermark', method='solid', box=(.5, .5, 1.0, 1.0))
    result, _ = run(work, png(grid()), ImageRecipe(output_format='png', regions=(item,)))
    assert result.watermark_quality_status == 'pending'
    assert result.privacy_status == 'pending'
    assert not result.watermark_restoration_performed and not result.license_confirmation


@pytest.mark.parametrize('loop,expected_plays', [(0, 0), (2, 3), (None, 1)])
def test_gif_all_frames_keep_alpha_delays_and_semantic_loop_count(work, loop, expected_plays):
    images = [Image.new('RGBA', (8, 8), c) for c in ('red', 'blue')]
    output = BytesIO()
    options = {} if loop is None else {'loop': loop}
    images[0].save(output, 'GIF', save_all=True, append_images=images[1:], duration=[70, 130], **options)
    raw = output.getvalue()
    recipe = ImageRecipe(output_format='apng', regions=(region(),))
    result, encoded = run(work, raw, recipe, name='synthetic.gif')
    before, after = decode(raw, ProcessingLimits()), decode(encoded, ProcessingLimits())
    assert result.frame_count == result.animation_frame_count == 2
    assert result.animation_plays == expected_plays
    assert before.delays == after.delays == (Fraction(7, 100), Fraction(13, 100))
    assert after.plays == expected_plays
    assert all(a.getchannel('A').tobytes() == b.getchannel('A').tobytes() for a, b in zip(before.frames, after.frames))


def test_identical_masked_frames_are_not_collapsed_or_retimed(work):
    frames = (Image.new('RGBA', (8, 8), 'red'), Image.new('RGBA', (8, 8), 'blue'))
    original = Decoded(frames, (Fraction(1, 60), Fraction(1, 120)), ('animation', 'animation'), 2, False, 'PNG', None)
    raw = encode(frames, original, 'apng', 1024**2)
    item = MaskRegion(region_id='entire-face', purpose='face', box=(0.0, 0.0, 1.0, 1.0), method='solid')
    result, encoded = run(work, raw, ImageRecipe(output_format='apng', regions=(item,)))
    actual = decode(encoded, ProcessingLimits())
    assert result.frame_count == 2 and actual.frames[0].tobytes() == actual.frames[1].tobytes()
    assert actual.delays == original.delays and actual.plays == 2
    with Image.open(BytesIO(encoded)) as independent:
        assert independent.n_frames == 2
        for index, expected in enumerate(original.delays):
            independent.seek(index)
            assert independent.info['duration']/1000 == pytest.approx(float(expected))


def test_apng_default_poster_is_masked_and_preserved_separately(work):
    frames = (Image.new('RGBA', (8, 8), 'yellow'), Image.new('RGBA', (8, 8), 'red'), Image.new('RGBA', (8, 8), 'blue'))
    original = Decoded(frames, (None, Fraction(1, 10), Fraction(1, 5)), ('poster', 'animation', 'animation'), 0, True, 'PNG', None)
    raw = encode(frames, original, 'apng', 1024**2)
    item = MaskRegion(region_id='entire-face', purpose='face', box=(0.0, 0.0, 1.0, 1.0), method='solid')
    result, encoded = run(work, raw, ImageRecipe(output_format='apng', regions=(item,)))
    actual = decode(encoded, ProcessingLimits())
    assert result.has_default_poster and result.frame_count == 3 and result.animation_frame_count == 2
    assert actual.poster and actual.delays == original.delays
    assert all(frame.getpixel((0, 0)) == (0, 0, 0, 255) for frame in actual.frames)


def test_partial_face_frame_selection_is_rejected_instead_of_claiming_entire_animation_processed(work):
    frames = (Image.new('RGBA', (8, 8), 'red'), Image.new('RGBA', (8, 8), 'blue'))
    original = Decoded(frames, (Fraction(1, 10),)*2, ('animation',)*2, 0, False, 'PNG', None)
    raw = encode(frames, original, 'apng', 1024**2)
    with pytest.raises(ImageProcessingError, match='cover_each_display_frame'):
        run(work, raw, ImageRecipe(output_format='apng', regions=(region(frame_end=0),)))


def test_frame_specific_regions_cover_each_frame_without_automatic_interpolation(work):
    frames = (Image.new('RGBA', (8, 8), 'red'), Image.new('RGBA', (8, 8), 'blue'))
    original = Decoded(frames, (Fraction(1, 10),)*2, ('animation',)*2, 0, False, 'PNG', None)
    raw = encode(frames, original, 'apng', 1024**2)
    first = region(frame_end=0)
    second = MaskRegion(region_id='other-position', purpose='face', box=(.5, .5, 1.0, 1.0), method='solid', frame_start=1, frame_end=1)
    result, _ = run(work, raw, ImageRecipe(output_format='apng', regions=(first, second)))
    assert result.frames[0].regions[0].region_id == first.region_id
    assert result.frames[1].regions[0].region_id == second.region_id


def test_source_hash_path_and_output_overwrite_guards(work):
    source, output = work
    raw = png(grid()); (source/'synthetic.png').write_bytes(raw)
    service = ImageProcessor(output)
    recipe = ImageRecipe(output_format='png', regions=(region(),))
    with pytest.raises(IntegrityError):
        service.process(FilesystemBackend(source), 'synthetic.png', expected_source_sha256='0'*64, job_id='synthetic', recipe=recipe)
    with pytest.raises(UnsafePath):
        service.process(FilesystemBackend(source, writable=True), 'synthetic.png', expected_source_sha256=sha256(raw).hexdigest(), job_id='synthetic', recipe=recipe)
    with pytest.raises(UnsafePath):
        ImageProcessor(source).process(FilesystemBackend(source), 'synthetic.png', expected_source_sha256=sha256(raw).hexdigest(), job_id='synthetic', recipe=recipe)
    result, _ = run(work, raw, recipe)
    original = (output/result.output_ref).read_bytes()
    with pytest.raises(ImageProcessingError, match='already_exists'):
        service.process(FilesystemBackend(source), 'synthetic.png', expected_source_sha256=sha256(raw).hexdigest(), job_id='synthetic-job', recipe=recipe)
    assert (output/result.output_ref).read_bytes() == original


@pytest.mark.parametrize('limit', ['input', 'pixels', 'output'])
def test_processing_budgets_fail_without_publishing_output(work, limit):
    options = {'max_input_bytes': 10} if limit == 'input' else {'max_pixels': 4} if limit == 'pixels' else {'max_output_bytes': 10}
    with pytest.raises(ImageProcessingError, match='budget'):
        run(work, png(grid()), ImageRecipe(output_format='png', regions=(region(),)), limits=ProcessingLimits(**options))
    assert not list(work[1].rglob('*.png'))


def test_orientation_and_embedded_rgb_profile_are_preserved_in_display_space(work):
    image = Image.new('RGB', (16, 8), 'red')
    profile = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
    exif = Image.Exif(); exif[274] = 6
    source = BytesIO(); image.save(source, 'JPEG', exif=exif, icc_profile=profile)
    result, encoded = run(work, source.getvalue(), ImageRecipe(output_format='png', regions=(region(),)), name='synthetic.jpg')
    actual = Image.open(BytesIO(encoded))
    assert actual.size == (8, 16) and result.source_exif_orientation == 6
    assert actual.info['icc_profile'] == profile and result.icc_profile_sha256 == sha256(profile).hexdigest()
    assert not actual.getexif()


def test_high_bit_depth_is_not_silently_reduced_to_eight_bit(work):
    raw = png(Image.new('I;16', (8, 8), 1000))
    with pytest.raises(ImageProcessingError, match='high_bit_depth'):
        run(work, raw, ImageRecipe(output_format='png', regions=(region(),)))


def test_lossless_webp_sticker_preserves_all_composed_frames_and_play_count(work):
    frames = [Image.new('RGBA', (8, 8), (255, 0, 0, 80)), Image.new('RGBA', (8, 8), (0, 0, 255, 180))]
    stream = BytesIO()
    frames[0].save(stream, 'WEBP', save_all=True, append_images=frames[1:], duration=[80, 120], loop=2, lossless=True)
    raw = stream.getvalue()
    result, encoded = run(work, raw, ImageRecipe(output_format='apng', regions=(region(),)), name='synthetic.webp')
    before, after = decode(raw, ProcessingLimits()), decode(encoded, ProcessingLimits())
    assert result.animation_plays == before.plays == after.plays == 2
    assert before.delays == after.delays == (Fraction(2, 25), Fraction(3, 25))
    assert all(a.getchannel('A').tobytes() == b.getchannel('A').tobytes() for a, b in zip(before.frames, after.frames))


def test_explicit_png_request_cannot_silently_drop_animation_frames(work):
    frames = (Image.new('RGBA', (8, 8), 'red'), Image.new('RGBA', (8, 8), 'blue'))
    original = Decoded(frames, (Fraction(1, 10),)*2, ('animation',)*2, 0, False, 'PNG', None)
    raw = encode(frames, original, 'apng', 1024**2)
    with pytest.raises(ImageProcessingError, match='animation_requires'):
        run(work, raw, ImageRecipe(output_format='png', regions=(region(),)))


def test_worker_staged_readonly_input_and_output_can_share_workspace_container(work):
    _, output = work
    inputs = output/'inputs'; inputs.mkdir(); (inputs/'README.md').write_text('Synthetic worker staging.\n')
    raw = png(grid()); (inputs/'synthetic.png').write_bytes(raw)
    result = ImageProcessor(output).process(FilesystemBackend(inputs), 'synthetic.png',
        expected_source_sha256=sha256(raw).hexdigest(), job_id='same-workspace-container',
        recipe=ImageRecipe(output_format='png', regions=(region(),)))
    assert result.output_sha256 == sha256((output/result.output_ref).read_bytes()).hexdigest()
    assert (inputs/'synthetic.png').read_bytes() == raw


@pytest.mark.parametrize('mutation', ['frame_count', 'path', 'privacy_approval', 'recipe'])
def test_report_contract_rejects_inconsistent_frame_path_or_fake_approval(work, mutation):
    result, _ = run(work, png(grid()), ImageRecipe(output_format='png', regions=(region(),)))
    data = result.model_dump(mode='json')
    if mutation == 'frame_count':
        data['frame_count'] = 99
    elif mutation == 'path':
        data['output_ref'] = '../original.png'
    elif mutation == 'recipe':
        data['recipe']['regions'][0]['strength'] = 64
    else:
        data['privacy_confirmation'] = True
    with pytest.raises(ValueError):
        ProcessingResult.model_validate_json(json.dumps(data))


def test_transparent_gif_disposal_is_materialized_without_leaving_previous_frame_pixels(work):
    frames = [Image.new('RGBA', (16, 12), (0, 0, 0, 0)) for _ in range(2)]
    ImageDraw.Draw(frames[0]).rectangle((1, 1, 3, 3), fill=(255, 0, 0, 255))
    ImageDraw.Draw(frames[1]).rectangle((12, 8, 14, 10), fill=(0, 0, 255, 255))
    source = BytesIO()
    frames[0].save(source, 'GIF', save_all=True, append_images=frames[1:], duration=[80, 120], loop=0,
        disposal=2, transparency=0, optimize=False)
    raw = source.getvalue()
    before = decode(raw, ProcessingLimits())
    assert before.frames[0].getpixel((2, 2))[3] == 255
    assert before.frames[1].getpixel((2, 2))[3] == 0
    _, encoded = run(work, raw, ImageRecipe(output_format='apng', regions=(region(),)), name='transparent-disposal.gif')
    after = decode(encoded, ProcessingLimits())
    for index in range(2):
        assert before.frames[index].getchannel('A').tobytes() == after.frames[index].getchannel('A').tobytes()
        assert before.frames[index].getpixel((2, 2)) == after.frames[index].getpixel((2, 2))
    assert after.frames[1].getpixel((2, 2))[3] == 0


def declared_animation(work):
    frames = (grid(), Image.new('RGBA', (16, 12), (20, 50, 100, 60)))
    original = Decoded(frames, (Fraction(1, 10), Fraction(1, 5)), ('animation', 'animation'), 0, False, 'PNG', None)
    raw = encode(frames, original, 'apng', 1024**2)
    (work[0]/'declared-animation.png').write_bytes(raw)
    service = ImageProcessor(work[1])
    source = FilesystemBackend(work[0])
    inspection = service.inspect(source, 'declared-animation.png', expected_source_sha256=sha256(raw).hexdigest())
    return service, source, raw, inspection


def test_explicit_unmasked_frame_declaration_preserves_frame_without_claiming_no_face_or_approval(work):
    service, source, raw, inspection = declared_animation(work)
    declaration = UnmaskedFrameDeclaration(frame_index=1, source_rgba_sha256=inspection.frames[1].source_rgba_sha256)
    recipe = ImageRecipe(output_format='apng', regions=(region(frame_end=0),), unmasked_frame_declarations=(declaration,))
    result = service.process(source, 'declared-animation.png', expected_source_sha256=inspection.source_sha256,
        job_id='explicit-unmasked', recipe=recipe)
    before = decode(raw, ProcessingLimits())
    after = decode((work[1]/result.output_ref).read_bytes(), ProcessingLimits())
    assert after.frames[1].tobytes() == before.frames[1].tobytes()
    assert after.frames[0].tobytes() != before.frames[0].tobytes()
    assert result.frames[1].unmasked_frame_declaration == declaration
    assert result.frames[1].regions == ()
    assert result.frames[1].source_rgba_sha256 == inspection.frames[1].source_rgba_sha256
    assert result.privacy_status == result.quality_status == 'pending'
    assert not result.face_detection_performed and not result.privacy_confirmation


@pytest.mark.parametrize('failure', ['wrong_hash', 'out_of_range', 'conflicting_face'])
def test_unmasked_frame_declarations_reject_wrong_version_range_or_conflicting_mask(work, failure):
    service, source, _, inspection = declared_animation(work)
    index = 2 if failure == 'out_of_range' else 1
    declaration = UnmaskedFrameDeclaration(frame_index=index,
        source_rgba_sha256='0'*64 if failure == 'wrong_hash' else inspection.frames[1].source_rgba_sha256)
    face = region() if failure == 'conflicting_face' else region(frame_end=0)
    with pytest.raises(ImageProcessingError, match='unmasked_frame_declaration'):
        service.process(source, 'declared-animation.png', expected_source_sha256=inspection.source_sha256,
            job_id='invalid-declaration', recipe=ImageRecipe(output_format='apng', regions=(face,),
                unmasked_frame_declarations=(declaration,)))
    assert not (work[1]/'jobs').exists()


def test_duplicate_unmasked_declarations_are_rejected(work):
    _, _, _, inspection = declared_animation(work)
    declaration = UnmaskedFrameDeclaration(frame_index=1, source_rgba_sha256=inspection.frames[1].source_rgba_sha256)
    with pytest.raises(ValueError, match='unique unmasked'):
        ImageRecipe(output_format='apng', regions=(region(frame_end=0),),
            unmasked_frame_declarations=(declaration, declaration))


def test_inspection_is_readonly_bounded_and_bound_to_actual_source(work):
    service, source, _, inspection = declared_animation(work)
    assert len(inspection.frames) == inspection.frame_count == 2
    assert not inspection.face_detection_performed and not inspection.privacy_confirmation
    assert sorted(p.relative_to(work[1]).as_posix() for p in work[1].rglob('*')) == ['README.md']
    with pytest.raises(IntegrityError):
        service.inspect(source, 'declared-animation.png', expected_source_sha256='0'*64)
    with pytest.raises(UnsafePath):
        service.inspect(FilesystemBackend(work[0], writable=True), 'declared-animation.png',
            expected_source_sha256=inspection.source_sha256)
    with pytest.raises(UnsafePath):
        service.inspect(source, '../outside.png', expected_source_sha256=inspection.source_sha256)
    with pytest.raises(ImageProcessingError, match='budget'):
        ImageProcessor(work[1], limits=ProcessingLimits(max_input_bytes=10)).inspect(source,
            'declared-animation.png', expected_source_sha256=inspection.source_sha256)


def test_default_poster_can_have_explicit_unmasked_declaration_while_animation_is_masked(work):
    frames = (Image.new('RGBA', (8, 8), 'yellow'), Image.new('RGBA', (8, 8), 'red'), Image.new('RGBA', (8, 8), 'blue'))
    original = Decoded(frames, (None, Fraction(1, 10), Fraction(1, 5)), ('poster', 'animation', 'animation'), 0, True, 'PNG', None)
    raw = encode(frames, original, 'apng', 1024**2)
    (work[0]/'poster.png').write_bytes(raw)
    service = ImageProcessor(work[1]); source = FilesystemBackend(work[0])
    inspection = service.inspect(source, 'poster.png', expected_source_sha256=sha256(raw).hexdigest())
    declaration = UnmaskedFrameDeclaration(frame_index=0, source_rgba_sha256=inspection.frames[0].source_rgba_sha256)
    result = service.process(source, 'poster.png', expected_source_sha256=inspection.source_sha256,
        job_id='poster-declaration', recipe=ImageRecipe(output_format='apng',
            regions=(region(frame_start=1),), unmasked_frame_declarations=(declaration,)))
    assert result.frames[0].role == 'poster' and result.frames[0].unmasked_frame_declaration == declaration
    assert not result.privacy_confirmation
