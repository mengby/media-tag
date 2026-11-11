from hashlib import sha256
from io import BytesIO
import json

from PIL import Image
import pytest

from search.conftest import sought, env
from media_tag.analysis.contracts import TOPIC_FIELDS, MEDICAL_FIELDS
from media_tag.contracts.models import Versions
from media_tag.local_topics import Generation, LocalQwenGenerator, LocalTopicService, Options
from media_tag.local_topics.parser import assess
from media_tag.local_topics.prompt import PROMPT_VERSION
from media_tag.review.models import ReviewDeployment
from media_tag.review.policy import ReviewPolicy
from media_tag.storage.filesystem import FilesystemBackend


def answer(field, values=(), *, dynamic=False, state=None):
    return json.dumps({'field': field, 'state': state or ('observed' if values else 'unknown'),
                       'values': list(values), 'dynamic': dynamic}, ensure_ascii=False)


class FakeGenerator:
    model_revision = 'a'*64
    def __init__(self, values=None):
        self.values = values or {}
        self.calls = []
        self.alter = {}
    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return Generation(backend='synthetic_protocol', model_revision=self.model_revision,
            prompt_version=PROMPT_VERSION, image_sha256=sha256(kwargs['image_bytes']).hexdigest(),
            answers=tuple(answer(field, self.values.get(field, ())) for field in TOPIC_FIELDS), **self.alter)


def run(sought, tmp_path, generator=None, **kwargs):
    workspace = tmp_path/'drafts'; workspace.mkdir(exist_ok=True)
    service = LocalTopicService(workspace, policy=sought.reviews.policy, generator=generator or FakeGenerator())
    return service, service.run(FilesystemBackend(sought.original.parent), sought.original.name,
        expected_source_sha256=sha256(sought.original.read_bytes()).hexdigest(), asset_id=sought.asset.asset_id,
        asset_revision=sought.asset.revision, analysis_id='local_topics_test', **kwargs)


def test_real_ten_field_draft_source_versions_and_human_preservation(sought, tmp_path):
    original = sought.original.read_bytes()
    before = sought.reviews.detail(sought.asset.asset_id, principal=sought.env.owner)
    generator = FakeGenerator({'用途大类': (sought.purpose,), '科室': ('合成科室-仅测试',),
        '器官/系统': ('未发布器官',), '画面主体': ('红色圆形',), '中文检索词': ('红色圆形',),
        '英文检索词': ('red circle',), '表现形式': ('2D插图',)})
    service, result = run(sought, tmp_path, generator)
    fields = {f.field:f for f in result.document.fields}
    assert tuple(fields) == TOPIC_FIELDS
    assert fields['画面主体'].value.values == ('红色圆形',)
    assert fields['科室'].requires_field_review and fields['科室'].next_step == 'human'
    assert fields['器官/系统'].value.state == 'no_match'
    assert fields['器官/系统'].candidates == ('未发布器官',)
    assert all(f.versions.model_revision == generator.model_revision for f in result.document.fields)
    assert all(f.next_step != 'none' for f in result.document.fields)
    assert not result.document.analysis_complete and not result.precision_exemption
    assert result.backend == 'synthetic_protocol'
    with service.backend.open_read(result.image.ref) as stream:
        assert sha256(stream.read()).hexdigest() == result.image.sha256
    with service.backend.open_read(result.raw_report.ref) as stream:
        report = json.load(stream)
    assert report['source_sha256'] == sha256(original).hexdigest()
    assert report['generation']['backend'] == 'synthetic_protocol'
    assert report['options']['max_pixels'] == Options().max_pixels
    assert report['input_content_kind'] == 'image'
    assert report['taxonomy_version'] == sought.reviews.policy.config.taxonomy_version
    assert set(generator.calls[0]) == {'image_bytes', 'field_prompts', 'options'}
    assert len(generator.calls[0]['field_prompts']) == 10
    assert all('synthetic.png' not in p and 'Unrelated original name' not in p for p in generator.calls[0]['field_prompts'])
    assert sought.original.read_bytes() == original
    assert sought.reviews.detail(sought.asset.asset_id, principal=sought.env.owner) == before


@pytest.mark.parametrize('raw', [
    '{}', '```json\n{}\n```', '{"field":"画面主体","field":"科室"}',
    answer('科室', ('x',)), answer('画面主体', ('x','x')),
    answer('画面主体', ('x',), state='unknown'), answer('画面主体', (), dynamic=True),
    '{"field":"画面主体","state":"observed","values":[1],"dynamic":false}',
    '{"field":"画面主体","state":"observed","values":["x"],"dynamic":false,"confidence":1}',
])
def test_malformed_fields_stay_failed(sought, raw):
    field = assess(raw, '画面主体', policy=sought.reviews.policy,
        versions=Versions(rules_version='test', preprocess_version='test'), evidence_id='test_image')
    assert field.value.state == 'failed' and field.next_step == 'precision'


def test_missing_taxonomy_preserves_raw_candidates(sought):
    policy = ReviewPolicy(sought.env.contract, ReviewDeployment())
    field = assess(answer('科室', ('合成科室-仅测试',)), '科室', policy=policy,
        versions=Versions(rules_version='test', preprocess_version='test'), evidence_id='test_image')
    assert field.value.state == 'no_match' and field.next_step == 'vocabulary'
    assert field.candidates == ('合成科室-仅测试',)


@pytest.mark.parametrize('name,phrases', [('中文检索词', ('红色圆形', '白色背景')),
                                         ('英文检索词', ('red circle', 'white background'))])
def test_keyword_phrases_preserve_original_scalar_text_contract(sought, name, phrases):
    field = assess(answer(name, phrases), name, policy=sought.reviews.policy,
        versions=Versions(rules_version='test', preprocess_version='test'), evidence_id='test_image')
    assert field.value.state == 'observed'
    expected = phrases if sought.reviews.policy.asset_fields[name].field_type in ('多选','关联') else ('；'.join(phrases),)
    assert field.value.values == expected


@pytest.mark.parametrize('phrases', [('红色圆形',), ('red circle', '白色背景')])
def test_chinese_text_is_not_adopted_as_english_keywords(sought, phrases):
    field=assess(answer('英文检索词',phrases),'英文检索词',policy=sought.reviews.policy,
        versions=Versions(rules_version='test',preprocess_version='test'),evidence_id='test_image')
    assert field.value.state=='unknown' and field.value.values is None
    assert field.reason=='english_keywords_require_english_text' and field.next_step=='precision'


def test_multiple_valid_single_select_values_remain_unresolved(sought):
    name='情绪/语气'
    definition=sought.reviews.policy.asset_fields[name]
    assert definition.field_type=='单选'
    choices=sought.reviews.policy.options(definition)
    assert len(choices)>=2
    field=assess(answer(name,tuple(choices[:2])),name,policy=sought.reviews.policy,
        versions=Versions(rules_version='test',preprocess_version='test'),evidence_id='test_image')
    assert field.value.state=='unknown' and field.value.values is None
    assert field.reason=='local_single_selection_ambiguous' and field.next_step=='precision'


@pytest.mark.parametrize('field,values,dynamic', [('过程/状态', ('正常',), False),
    ('过程/状态', ('恶化→改善',), False), ('画面主体', ('旋转',), True)])
def test_static_does_not_prove_process(sought, field, values, dynamic):
    assessment = assess(answer(field, values, dynamic=dynamic), field, policy=sought.reviews.policy,
        versions=Versions(rules_version='test', preprocess_version='test'), evidence_id='test_image')
    assert assessment.value.state == 'unknown' and assessment.next_step == 'evidence'
    assert not assessment.candidates and not assessment.dynamic


def test_alpha_is_composited_and_source_not_changed(sought, tmp_path):
    image = Image.new('RGBA', (2,2), (255,0,0,0)); image.putpixel((0,0), (0,0,255,255))
    image.save(sought.original)
    raw = sought.original.read_bytes()
    generator = FakeGenerator()
    run(sought, tmp_path, generator, content_kind='sticker')
    with Image.open(BytesIO(generator.calls[0]['image_bytes'])) as output:
        assert output.mode == 'RGB' and output.getpixel((1,1)) == (255,255,255)
        assert output.getpixel((0,0)) == (0,0,255)
    assert sought.original.read_bytes() == raw


def test_animation_rejected_before_generator(sought, tmp_path):
    Image.new('RGB',(2,2),'red').save(sought.original, format='PNG', save_all=True,
        append_images=[Image.new('RGB',(2,2),'blue')], duration=[100,100], loop=0)
    generator = FakeGenerator()
    with pytest.raises(ValueError, match='animated_input'):
        run(sought, tmp_path, generator)
    assert not generator.calls


def test_source_hash_escape_and_overlap(sought, tmp_path):
    workspace = tmp_path/'drafts';workspace.mkdir()
    generator = FakeGenerator()
    service = LocalTopicService(workspace, policy=sought.reviews.policy, generator=generator)
    args = dict(expected_source_sha256='b'*64, asset_id=sought.asset.asset_id,
                asset_revision=sought.asset.revision, analysis_id='test')
    with pytest.raises(ValueError, match='source_hash'):
        service.run(FilesystemBackend(sought.original.parent), sought.original.name, **args)
    with pytest.raises(Exception):
        service.run(FilesystemBackend(sought.original.parent), '../outside.png', **args)
    with pytest.raises(ValueError, match='disjoint'):
        service.run(FilesystemBackend(workspace), 'anything.png', **args)
    assert not generator.calls


def test_native_disabled_and_no_gpu_probe(tmp_path):
    with pytest.raises(ValueError, match='explicit_boolean'):
        LocalQwenGenerator(tmp_path, enabled='false')
    generator = LocalQwenGenerator(tmp_path)
    report = generator.readiness()
    assert not report['model_executed'] and not report['enabled']
    assert report['model'] == 'Qwen/Qwen3-VL-2B-Instruct'
    with pytest.raises(RuntimeError, match='not_enabled'):
        generator(image_bytes=b'',field_prompts=(),options=Options())


@pytest.mark.parametrize('changed', ['image_sha256', 'model_revision', 'prompt_version'])
def test_generation_binding_is_verified(sought, tmp_path, changed):
    class WrongBinding(FakeGenerator):
        def __call__(self, **kwargs):
            original = super().__call__(**kwargs)
            return original.model_copy(update={changed: 'b'*64 if changed != 'prompt_version' else 'wrong-prompt'})
    with pytest.raises(ValueError, match='provenance_mismatch'):
        run(sought, tmp_path, WrongBinding())


def test_exif_orientation_is_normalized_and_removed(sought, tmp_path):
    image = Image.new('RGB', (3,2), 'white'); image.putpixel((0,0), (255,0,0))
    exif = Image.Exif();exif[274] = 6
    image.save(sought.original, exif=exif)
    generator = FakeGenerator();run(sought, tmp_path, generator)
    with Image.open(BytesIO(generator.calls[0]['image_bytes'])) as output:
        assert output.size == (2,3) and output.getpixel((1,0)) == (255,0,0)
        assert not output.getexif()


def test_source_pixel_budget_rejects_before_model(sought, tmp_path):
    workspace = tmp_path/'drafts';workspace.mkdir()
    generator = FakeGenerator()
    service = LocalTopicService(workspace, policy=sought.reviews.policy, generator=generator,
                                options=Options(max_source_pixels=1))
    with pytest.raises(ValueError, match='pixel_budget'):
        service.run(FilesystemBackend(sought.original.parent), sought.original.name,
            expected_source_sha256=sha256(sought.original.read_bytes()).hexdigest(), asset_id=sought.asset.asset_id,
            asset_revision=sought.asset.revision, analysis_id='test')
    assert not generator.calls and not list(workspace.iterdir())


def test_constructor_and_overlap_rejection_never_write_source(sought):
    before = set(sought.original.parent.iterdir())
    service = LocalTopicService(sought.original.parent, policy=sought.reviews.policy, generator=FakeGenerator())
    with pytest.raises(ValueError, match='disjoint'):
        service.run(FilesystemBackend(sought.original.parent), sought.original.name,
            expected_source_sha256=sha256(sought.original.read_bytes()).hexdigest(), asset_id=sought.asset.asset_id,
            asset_revision=sought.asset.revision, analysis_id='test')
    assert set(sought.original.parent.iterdir()) == before


def test_native_adapter_reuses_pinned_local_loader_protocol_without_gpu(tmp_path, monkeypatch):
    """Fully mocked native modules: verifies wiring, never proves real inference."""
    from contextlib import nullcontext
    import sys
    from types import SimpleNamespace
    import media_tag.models.native
    import media_tag.semantic.processor
    import media_tag.semantic.patch_projection
    import media_tag.local_topics.native as native
    events = []
    class Tensor:
        shape = (1, 20)
        def __getitem__(self, key):return self
    class Inputs(dict):
        def to(self, device):
            assert device == 'cuda';return self
    class Processor:
        def apply_chat_template(self, messages, **kwargs):
            assert len(messages) == 2 and messages[1]['content'][0]['type'] == 'image'
            events.append(('prompt', messages[1]['content'][1]['text']))
            return Inputs(input_ids=Tensor(), image_grid_thw=[(1, 8, 8)])
        def batch_decode(self, generated, **kwargs):return ['mock native answer']
    class Model:
        @classmethod
        def from_pretrained(cls, root, **kwargs):
            assert kwargs['local_files_only'] is True and kwargs['trust_remote_code'] is False
            events.append(('load',root));return cls()
        def eval(self):return self
        def generate(self, **kwargs):
            assert kwargs['do_sample'] is False and kwargs['max_new_tokens'] == 192
            return Tensor()
    fake_torch = SimpleNamespace(bfloat16='bf16', set_num_threads=lambda _:None, manual_seed=lambda _:None,
        inference_mode=nullcontext, cuda=SimpleNamespace(is_available=lambda:True,
        is_bf16_supported=lambda:True, set_per_process_memory_fraction=lambda _:None,
        synchronize=lambda:None, empty_cache=lambda:None))
    monkeypatch.setitem(sys.modules, 'torch', fake_torch)
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(Qwen3VLForConditionalGeneration=Model))
    monkeypatch.setattr(media_tag.models.native, 'prepare_native_runtime', lambda:None)
    monkeypatch.setattr(media_tag.semantic.processor, 'make_processor', lambda root,pixels:Processor())
    monkeypatch.setattr(media_tag.semantic.patch_projection, 'replace_patch_projection', lambda model:None)
    generator = LocalQwenGenerator(tmp_path, enabled=True)
    monkeypatch.setattr(generator.registry, 'verify', lambda:tmp_path)
    image = BytesIO();Image.new('RGB',(2,2),'white').save(image, format='PNG')
    result = generator(image_bytes=image.getvalue(), field_prompts=tuple(TOPIC_FIELDS), options=Options())
    assert len(result.answers) == 10 and len([x for x in events if x[0]=='load']) == 1
    assert [x[1] for x in events if x[0]=='prompt'] == list(TOPIC_FIELDS)
    assert result.image_sha256 == sha256(image.getvalue()).hexdigest()
