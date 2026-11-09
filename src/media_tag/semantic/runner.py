"""One actual local VLM invocation. No network, filename, catalogue writes or remote code."""
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from media_tag.models.contracts import ModelFailure,FrameInput
from media_tag.models.native import prepare_native_runtime
from media_tag.models.lifecycle import watch_parent
from media_tag.storage.filesystem import FilesystemBackend
from .contracts import SemanticOptions,parse_caption
from .registry import SemanticRegistry
from .prompt import SYSTEM,FIELD_PROMPTS,PROMPT_VERSION
from .processor import make_processor
from .stopping import criteria
from .patch_projection import replace_patch_projection


def execute(request,*,diagnostic_path=None):
    total_started=time.monotonic()
    prepare_native_runtime()
    from PIL import Image
    import torch
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    registry=SemanticRegistry(Path(request['model_root']))
    if registry.revision!=request['model_revision'] or request['prompt_version']!=PROMPT_VERSION:raise ModelFailure('semantic_version_mismatch')
    registry.verify();verified_at=time.monotonic();options=SemanticOptions.model_validate(request['options'])
    frame=FrameInput.model_validate(request['frame']);backend=FilesystemBackend(Path(request['workspace']))
    if frame.source_sha256!=request['source_sha256']:raise ModelFailure('frame_binding_mismatch')
    with backend.open_read(frame.artifact.ref) as stream:raw=stream.read(16*1024**2+1)
    if len(raw)>16*1024**2 or sha256(raw).hexdigest()!=frame.artifact.sha256:raise ModelFailure('frame_integrity')
    from io import BytesIO
    with Image.open(BytesIO(raw)) as original:
        if original.format!='PNG' or original.width*original.height>2048**2:raise ModelFailure('pixel_budget')
        image=original.convert('RGB')
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():raise ModelFailure('cuda_unavailable')
    torch.set_num_threads(1);torch.manual_seed(0);torch.cuda.set_per_process_memory_fraction(.75)
    torch.cuda.reset_peak_memory_stats();started=time.monotonic()
    processor=make_processor(registry.root,options.max_pixels)
    model=Qwen3VLForConditionalGeneration.from_pretrained(registry.root,local_files_only=True,trust_remote_code=False,
        dtype=torch.bfloat16,attn_implementation='sdpa',device_map={'':'cuda:0'},low_cpu_mem_usage=True).eval()
    projection=replace_patch_projection(model)
    loaded_at=time.monotonic()
    processor.tokenizer.padding_side='left'
    messages=[[{'role':'system','content':[{'type':'text','text':SYSTEM}]},
        {'role':'user','content':[{'type':'image','image':image},{'type':'text','text':prompt}]}] for prompt in FIELD_PROMPTS]
    inputs=processor.apply_chat_template(messages,add_generation_prompt=True,tokenize=True,padding=True,return_dict=True,return_tensors='pt').to('cuda')
    prepared_at=time.monotonic()
    processed_pixels=[int(v[1])*int(v[2])*16**2 for v in inputs['image_grid_thw'].cpu().tolist()]
    if any(v>options.max_pixels for v in processed_pixels):raise ModelFailure('semantic_pixel_budget_not_applied')
    if inputs['input_ids'].shape[1]>2048:raise ModelFailure('semantic_input_budget')
    with torch.inference_mode():output=model.generate(**inputs,max_new_tokens=min(48,options.max_new_tokens),do_sample=False,
        stopping_criteria=criteria(processor,inputs['input_ids'].shape[1]))
    generated=output[:,inputs['input_ids'].shape[1]:]
    generated_at=time.monotonic()
    answers=[v.strip() for v in processor.batch_decode(generated,skip_special_tokens=True,clean_up_tokenization_spaces=False)]
    if len(answers)!=4:raise ModelFailure('semantic_invalid_output')
    import re
    missing=lambda v:v.strip() in ('未知','无法确定','无法判断','无法识别','Unknown','unknown','N/A','')
    data={'subject':'' if missing(answers[0]) else answers[0],'form':'' if missing(answers[1]) else answers[1],
        'keywords_zh':[] if missing(answers[2]) else re.split('[,，、;；\n]+',answers[2]),
        'keywords_en':[] if missing(answers[3]) else re.split('[,，;；\n]+',answers[3]),'uncertainties':[]}
    text=json.dumps(data,ensure_ascii=False)
    if diagnostic_path is not None:Path(diagnostic_path).write_text(text,encoding='utf8')
    try:caption,normalizations=parse_caption(text)
    except ValueError:raise ModelFailure('semantic_invalid_output') from None
    torch.cuda.synchronize()
    return {'kind':'visual_fields','model_revision':registry.revision,'upstream_revision':registry.manifest['revision'],
        'prompt_version':PROMPT_VERSION,'source_sha256':request['source_sha256'],'frame':frame.model_dump(mode='json'),
        'caption':caption.model_dump(mode='json'),'raw_text':text,'raw_answers':answers,'normalizations':normalizations,'elapsed_seconds':time.monotonic()-started,
        'peak_gpu_allocated_bytes':torch.cuda.max_memory_allocated(),'medical_confirmation':False,'effective_values_written':False,
        'full_video_observed':False,'synonym_mapping_published':False,'calibration_state':'not_calibrated',
        'phase_seconds':{'imports_and_verify':verified_at-total_started,'processor_and_model_load':loaded_at-started,
            'prepare_inputs':prepared_at-loaded_at,'generate':generated_at-prepared_at},'generated_tokens':int(generated.shape[1]),
        'processor_budget_policy':'size-v2','processed_pixels':processed_pixels,'decoding_policy':'three-complete-phrases-v1',
        'patch_projection':projection}


def main():
    request=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'));watch_parent(request['executor_pid'])
    try:
        if request.get('prefetch'):
            from .native_batch import execute as batch_execute
            reports=batch_execute(request,diagnostic_path=Path(sys.argv[2]).with_name('raw-output.txt'))
            result={'ok':True,'data':reports[0]['report'],'prefetched':reports[1:]}
        else:result={'ok':True,'data':execute(request,diagnostic_path=Path(sys.argv[2]).with_name('raw-output.txt'))}
    except Exception as exc:
        code=exc.code if isinstance(exc,ModelFailure) else 'oom' if type(exc).__name__ in ('OutOfMemoryError','CUDAOutOfMemoryError') else 'semantic_execution_failed'
        result={'ok':False,'error_code':code}
    Path(sys.argv[2]).write_text(json.dumps(result,sort_keys=True,allow_nan=False),encoding='utf8')
    return 0 if result['ok'] else 2


if __name__=='__main__':raise SystemExit(main())
