"""Native video processor protocol entirely mocked: no torch/GPU/weights execution."""
from contextlib import nullcontext
from io import BytesIO
import sys
from types import SimpleNamespace

from PIL import Image
import pytest

from media_tag.temporal_topics import Options,LocalQwenVideoGenerator
from media_tag.temporal_topics.contracts import Rational
from media_tag.temporal_topics.native import input_digest


def test_native_passes_all_ordered_frames_video_metadata_and_disables_sampling(tmp_path,monkeypatch):
    import media_tag.models.native
    import media_tag.semantic.processor
    import media_tag.semantic.patch_projection
    import media_tag.temporal_topics.native as native
    events=[]
    class Tensor:
        shape=(1,20)
        def __getitem__(self,key):return self
    class Inputs(dict):
        def to(self,device):return self
    class Processor:
        def apply_chat_template(self,messages,**kwargs):
            assert kwargs['tokenize'] is False and messages[1]['content'][0]=={'type':'video'}
            return 'mock <video> prompt'
        def __call__(self,**kwargs):
            assert kwargs['do_sample_frames'] is False and kwargs['do_resize'] is False
            video=kwargs['videos'][0]
            assert video.shape==(5,128,128,3)
            assert [int(frame[0,0,0]) for frame in video]==[255,255,0,0,0]
            metadata=kwargs['video_metadata'][0]
            assert metadata['fps']==25 and metadata['frames_indices']==list(range(5))
            assert metadata['duration']==.2
            events.append('video')
            return Inputs(input_ids=Tensor(),video_grid_thw=[(3,8,8)])
        def batch_decode(self,output,**kwargs):return ['mock answer']
    class Model:
        @classmethod
        def from_pretrained(cls,root,**kwargs):
            assert kwargs['local_files_only'] and not kwargs['trust_remote_code'];events.append('load');return cls()
        def eval(self):return self
        def generate(self,**kwargs):return Tensor()
    torch=SimpleNamespace(bfloat16='bf16',set_num_threads=lambda _:None,manual_seed=lambda _:None,inference_mode=nullcontext,
        cuda=SimpleNamespace(is_available=lambda:True,is_bf16_supported=lambda:True,
            set_per_process_memory_fraction=lambda _:None,synchronize=lambda:None,empty_cache=lambda:None))
    monkeypatch.setitem(sys.modules,'torch',torch)
    monkeypatch.setitem(sys.modules,'transformers',SimpleNamespace(Qwen3VLForConditionalGeneration=Model))
    monkeypatch.setattr(media_tag.models.native,'prepare_native_runtime',lambda:None)
    monkeypatch.setattr(media_tag.semantic.processor,'make_processor',lambda root,pixels:Processor())
    monkeypatch.setattr(media_tag.semantic.patch_projection,'replace_patch_projection',lambda model:None)
    monkeypatch.setattr(native,'_processor_identity',lambda:'fully_mocked_protocol_not_actual_transformers_execution')
    frames=[]
    for color in ('red','red','blue','blue','blue'):
        output=BytesIO();Image.new('RGB',(128,128),color).save(output,format='PNG');frames.append(output.getvalue())
    frames=tuple(frames);fps=Rational.of(25);duration=Rational.of('1/5')
    generator=LocalQwenVideoGenerator(tmp_path,enabled=True)
    monkeypatch.setattr(generator.registry,'verify',lambda:tmp_path)
    result=generator(frame_bytes=frames,fps=fps,duration=duration,input_sha256=input_digest(frames,fps,duration),
        field_prompts=tuple(str(i) for i in range(10)),options=Options())
    assert len(result.answers)==10 and events.count('video')==10 and events.count('load')==1
