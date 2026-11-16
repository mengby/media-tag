"""Validate the native processor conversation boundary without loading a model."""
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
import json
import sys

from PIL import Image
import pytest

from media_tag.local_topics.contracts import Options
from media_tag.local_topics.native import LocalQwenGenerator


def test_native_processor_receives_typed_content_for_every_role(monkeypatch,tmp_path):
    calls=[]
    class Inputs(dict):
        def to(self,device):return self
    class Generated:
        def __getitem__(self,key):return []
    class Processor:
        def apply_chat_template(self,messages,**kwargs):
            # The multimodal processor iterates content blocks for every role,
            # including system. A plain system string fails this API contract.
            for message in messages:
                assert isinstance(message['content'],list)
                for block in message['content']:
                    assert block['type'] in ('text','image')
                    if block['type']=='text':assert isinstance(block['text'],str)
            assert sum(block['type']=='image' for m in messages for block in m['content'])==1
            calls.append(messages)
            return Inputs(image_grid_thw=[[1,8,8]],input_ids=SimpleNamespace(shape=(1,5)))
        def batch_decode(self,*args,**kwargs):return ['synthetic native contract reply']
    class Model:
        def eval(self):return self
        def generate(self,**kwargs):return Generated()
    loader=SimpleNamespace(from_pretrained=lambda *args,**kwargs:Model())
    cuda=SimpleNamespace(is_available=lambda:True,is_bf16_supported=lambda:True,
        set_per_process_memory_fraction=lambda *args:None,synchronize=lambda:None,empty_cache=lambda:None)
    monkeypatch.setitem(sys.modules,'torch',SimpleNamespace(cuda=cuda,bfloat16='synthetic-bfloat16',
        set_num_threads=lambda *args:None,manual_seed=lambda *args:None,inference_mode=nullcontext))
    monkeypatch.setitem(sys.modules,'transformers',SimpleNamespace(Qwen3VLForConditionalGeneration=loader))
    monkeypatch.setitem(sys.modules,'media_tag.models.native',SimpleNamespace(prepare_native_runtime=lambda:None))
    monkeypatch.setitem(sys.modules,'media_tag.semantic.processor',SimpleNamespace(make_processor=lambda *args:Processor()))
    monkeypatch.setitem(sys.modules,'media_tag.semantic.patch_projection',SimpleNamespace(replace_patch_projection=lambda *args:None))
    generator=LocalQwenGenerator(tmp_path,enabled=True)
    monkeypatch.setattr(generator.registry,'verify',lambda:None)
    image=BytesIO();Image.new('RGB',(24,24),'red').save(image,format='PNG')
    result=generator(image_bytes=image.getvalue(),field_prompts=tuple('synthetic field' for _ in range(10)),options=Options(max_pixels=128**2))
    assert len(calls)==len(result.answers)==10


@pytest.mark.parametrize('scenario', ['oom', 'nine', 'yield'])
def test_native_batch_shares_weights_and_matches_each_image_to_each_field(monkeypatch, tmp_path, scenario):
    calls = []
    loads = []
    class FixtureOOM(RuntimeError):
        pass
    class Inputs(dict):
        def to(self, device):
            return self
    class Tokens:
        def __getitem__(self, selector):
            return self
    class Processor:
        tokenizer = SimpleNamespace(padding_side='right')
        def apply_chat_template(self, conversations, *, padding, **kwargs):
            if not padding:
                conversations = [conversations]
            pairs = [(chat[1]['content'][0]['image'].getpixel((0, 0))[0],
                      chat[1]['content'][1]['text']) for chat in conversations]
            assert len({field for _, field in pairs}) == 1
            assert len(pairs) == 1 or self.tokenizer.padding_side == 'left'
            calls.append(pairs)
            return Inputs(image_grid_thw=[(1, 8, 8)] * len(pairs),
                          input_ids=SimpleNamespace(shape=(len(pairs), 8)))
        def batch_decode(self, *args, **kwargs):
            answers = []
            for red, field in calls[-1]:
                values = [] if red == 20 and field == 'form' else [f'{red}/{field}']
                if red == 40 and field == 'subject' and len(calls[-1]) > 1:
                    values.append('extra value')
                answers.append(json.dumps({'field':field, 'values':values, 'dynamic':False,
                                           'state':'observed' if values else 'unknown'}))
            return answers
    class Model:
        def eval(self):
            return self
        def generate(self, **kwargs):
            if scenario == 'oom' and len(calls[-1]) > 2:
                raise FixtureOOM()
            return Tokens()
    def load(*args, **kwargs):
        loads.append(True)
        return Model()
    cuda = SimpleNamespace(is_available=lambda: True, is_bf16_supported=lambda: True,
        set_per_process_memory_fraction=lambda *args: None, synchronize=lambda: None, empty_cache=lambda: None)
    monkeypatch.setitem(sys.modules, 'torch', SimpleNamespace(cuda=cuda, bfloat16='bf16',
        set_num_threads=lambda *args: None, manual_seed=lambda *args: None,
        inference_mode=nullcontext, OutOfMemoryError=FixtureOOM))
    monkeypatch.setitem(sys.modules, 'transformers', SimpleNamespace(
        Qwen3VLForConditionalGeneration=SimpleNamespace(from_pretrained=load)))
    monkeypatch.setitem(sys.modules, 'media_tag.models.native', SimpleNamespace(prepare_native_runtime=lambda: None))
    monkeypatch.setitem(sys.modules, 'media_tag.semantic.processor', SimpleNamespace(make_processor=lambda *args: Processor()))
    monkeypatch.setitem(sys.modules, 'media_tag.semantic.patch_projection', SimpleNamespace(replace_patch_projection=lambda model: None))
    images = []
    reds = list(range(20, 181, 20))
    for red in reds:
        buffer = BytesIO()
        Image.new('RGB', (4, 4), (red, 0, 0)).save(buffer, format='PNG')
        images.append(buffer.getvalue())
    generator = LocalQwenGenerator(tmp_path, enabled=True)
    monkeypatch.setattr(generator.registry, 'verify', lambda: None)
    outputs = generator.generate_batch(images=images, field_prompts=('subject', 'form'), options=Options(),
        field_rules=(('subject', '文本', False), ('form', '单选', False)),
        yield_requested=lambda: scenario == 'yield')
    assert len(loads) == 1
    widths = [len(group) for group in calls]
    if scenario == 'oom':
        assert widths == [4, 2, 1, 2, 2, 2, 2, 2, 2, 2, 1, 1]
    elif scenario == 'nine':
        assert widths == [4, 1, 4, 4, 4, 1, 1]
    else:
        assert widths == [4, 1, 4]
        reds = reds[:4]
    for red, output in zip(reds, outputs, strict=True):
        assert json.loads(output.answers[0])['values'] == [f'{red}/subject']
        assert json.loads(output.answers[1])['values'] == ([] if red == 20 else [f'{red}/form'])
        assert list(output.retried_answers) == (['subject'] if red == 40 else [])
