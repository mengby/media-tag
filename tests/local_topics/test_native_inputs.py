"""Validate the native processor conversation boundary without loading a model."""
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace
import sys

from PIL import Image

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
