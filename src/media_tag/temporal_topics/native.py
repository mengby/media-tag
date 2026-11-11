from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path

from media_tag.semantic.registry import SemanticRegistry
from .contracts import Generation,Rational
from .prompt import SYSTEM,PROMPT_VERSION


def _processor_identity():
    from importlib.metadata import distribution,version
    # Fingerprinting processor source must not import transformers (and torch)
    # into the API/worker parent or a decode-only verification process.
    package=Path(distribution('transformers').locate_file('transformers'))
    paths=('models/qwen3_vl/processing_qwen3_vl.py','models/qwen3_vl/video_processing_qwen3_vl.py',
           'video_processing_utils.py','video_utils.py')
    digest=sha256()
    for path in paths:digest.update(package.joinpath(path).read_bytes())
    return 'transformers-'+version('transformers')+'-'+digest.hexdigest()


def input_digest(frame_bytes,fps,duration):
    return sha256(json.dumps({'frames':[sha256(b).hexdigest() for b in frame_bytes],
        'fps':fps.model_dump(),'duration':duration.model_dump(),'policy':'all_frames_exact_hold_grid_v1'},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()


class LocalQwenVideoGenerator:
    def __init__(self,model_root,*,enabled=False):
        if type(enabled) is not bool:raise ValueError('explicit_boolean_execution_flag_required')
        self.registry=SemanticRegistry(Path(model_root));self.enabled=enabled
    @property
    def model_revision(self):return self.registry.revision
    def readiness(self):
        return {'enabled':self.enabled,'model':'Qwen/Qwen3-VL-2B-Instruct','model_revision':self.model_revision,
            'model_executed':False,'requirements':['exclusive GPU lease','bounded child process and timeout',
            'pinned local weights verification','continuous-video business validation pending']}
    def __call__(self,*,frame_bytes,fps,duration,input_sha256,field_prompts,options):
        if not self.enabled:raise RuntimeError('temporal_native_execution_not_enabled')
        if (not 2<=len(frame_bytes)<=options.max_model_frames or len(field_prompts)!=10
                or fps.fraction()<=0 or duration.fraction()<=0 or len(frame_bytes)/fps.fraction()!=duration.fraction()
                or any(len(p)>14000 for p in field_prompts) or input_digest(frame_bytes,fps,duration)!=input_sha256):
            raise ValueError('temporal_input_binding')
        from media_tag.models.native import prepare_native_runtime
        prepare_native_runtime()
        from PIL import Image
        import numpy as np
        import torch
        from transformers import Qwen3VLForConditionalGeneration
        from media_tag.semantic.processor import make_processor
        from media_tag.semantic.patch_projection import replace_patch_projection
        self.registry.verify()
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():raise RuntimeError('temporal_cuda_bfloat16_required')
        arrays=[]
        for data in frame_bytes:
            with Image.open(BytesIO(data)) as image:
                if image.format!='PNG' or image.mode!='RGB' or image.size!=(options.model_edge,options.model_edge):
                    raise ValueError('temporal_model_frame_format')
                arrays.append(np.asarray(image).copy())
        video=np.stack(arrays)
        torch.set_num_threads(1);torch.manual_seed(0);torch.cuda.set_per_process_memory_fraction(.75)
        processor=make_processor(self.registry.root,128**2)
        processor_identity=_processor_identity()
        model=Qwen3VLForConditionalGeneration.from_pretrained(self.registry.root,local_files_only=True,
            trust_remote_code=False,dtype=torch.bfloat16,attn_implementation='sdpa',device_map={'':'cuda:0'},low_cpu_mem_usage=True).eval()
        replace_patch_projection(model);answers=[]
        try:
            for prompt in field_prompts:
                messages=[{'role':'system','content':SYSTEM},{'role':'user','content':[{'type':'video'},{'type':'text','text':prompt}]}]
                text=processor.apply_chat_template(messages,tokenize=False,add_generation_prompt=True)
                inputs=processor(text=[text],videos=[video],video_metadata=[{'total_num_frames':len(arrays),
                    'fps':float(fps.fraction()),'duration':float(duration.fraction()),'width':options.model_edge,
                    'height':options.model_edge,'frames_indices':list(range(len(arrays)))}],
                    do_sample_frames=False,do_resize=False,return_tensors='pt').to('cuda')
                grid=inputs['video_grid_thw']
                expected_temporal=(len(arrays)+1)//2
                if (len(grid)!=1 or int(grid[0][0])!=expected_temporal
                        or int(grid[0][1])*int(grid[0][2])*16**2!=options.model_edge**2
                        or inputs['input_ids'].shape[1]>6000):raise ValueError('temporal_processor_budget_or_frame_drop')
                with torch.inference_mode():
                    output=model.generate(**inputs,max_new_tokens=options.max_new_tokens,do_sample=False)
                answers.append(processor.batch_decode(output[:,inputs['input_ids'].shape[1]:],skip_special_tokens=True,
                    clean_up_tokenization_spaces=False)[0]);del output,inputs
            torch.cuda.synchronize()
            return Generation(backend='local_qwen_video',model_revision=self.model_revision,prompt_version=PROMPT_VERSION,
                input_sha256=input_sha256,answers=tuple(answers),processor_identity=processor_identity)
        finally:
            del model,processor;torch.cuda.empty_cache()
