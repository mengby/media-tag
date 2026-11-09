"""Bounded same-owner image microbatch; one model load and one GPU process."""
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import re
import time
from media_tag.models.contracts import FrameInput,ModelFailure
from media_tag.models.native import prepare_native_runtime
from media_tag.storage.filesystem import FilesystemBackend
from .contracts import SemanticOptions,parse_caption
from .prompt import SYSTEM,FIELD_PROMPTS,PROMPT_VERSION
from .registry import SemanticRegistry
from .processor import make_processor
from .stopping import criteria
from .patch_projection import replace_patch_projection


def execute(request,*,diagnostic_path=None):
    started=time.monotonic();prepare_native_runtime()
    from PIL import Image
    import torch
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    registry=SemanticRegistry(Path(request['model_root']))
    if registry.revision!=request['model_revision'] or request['prompt_version']!=PROMPT_VERSION:raise ModelFailure('semantic_version_mismatch')
    registry.verify();verified=time.monotonic()
    options=SemanticOptions.model_validate(request['options']);items=[request,*request.get('prefetch',[])]
    if not 1<=len(items)<=4:raise ModelFailure('semantic_batch_budget')
    backend=FilesystemBackend(Path(request['workspace']));valid=[]
    for index,item in enumerate(items):
        try:
            frame=FrameInput.model_validate(item['frame'])
            if frame.source_sha256!=item['source_sha256']:raise ModelFailure('frame_binding_mismatch')
            with backend.open_read(frame.artifact.ref) as stream:raw=stream.read(16*1024**2+1)
            if len(raw)>16*1024**2 or sha256(raw).hexdigest()!=frame.artifact.sha256:raise ModelFailure('frame_integrity')
            with Image.open(BytesIO(raw)) as image:
                if image.format!='PNG' or image.width*image.height>2048**2:raise ModelFailure('pixel_budget')
                valid.append((item,frame,image.convert('RGB')))
        except Exception:
            if index==0:raise
            # An invalid queued candidate must not fail the current leased task.
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():raise ModelFailure('cuda_unavailable')
    torch.set_num_threads(1);torch.manual_seed(0);torch.cuda.set_per_process_memory_fraction(.75);torch.cuda.reset_peak_memory_stats()
    processor=make_processor(registry.root,options.max_pixels)
    model=Qwen3VLForConditionalGeneration.from_pretrained(registry.root,local_files_only=True,trust_remote_code=False,
        dtype=torch.bfloat16,attn_implementation='sdpa',device_map={'':'cuda:0'},low_cpu_mem_usage=True).eval()
    projection=replace_patch_projection(model)
    loaded=time.monotonic();processor.tokenizer.padding_side='left'
    messages=[[{'role':'system','content':[{'type':'text','text':SYSTEM}]},
        {'role':'user','content':[{'type':'image','image':image},{'type':'text','text':prompt}]}]
        for _,_,image in valid for prompt in FIELD_PROMPTS]
    inputs=processor.apply_chat_template(messages,add_generation_prompt=True,tokenize=True,padding=True,return_dict=True,return_tensors='pt').to('cuda')
    if inputs['input_ids'].shape[1]>2048:raise ModelFailure('semantic_input_budget')
    processed_pixels=[int(v[1])*int(v[2])*16**2 for v in inputs['image_grid_thw'].cpu().tolist()]
    if any(v>options.max_pixels for v in processed_pixels):raise ModelFailure('semantic_pixel_budget_not_applied')
    prepared=time.monotonic()
    with torch.inference_mode():output=model.generate(**inputs,max_new_tokens=min(48,options.max_new_tokens),do_sample=False,
        stopping_criteria=criteria(processor,inputs['input_ids'].shape[1]))
    tokens=output[:,inputs['input_ids'].shape[1]:];torch.cuda.synchronize();generated=time.monotonic()
    answers=[v.strip() for v in processor.batch_decode(tokens,skip_special_tokens=True,clean_up_tokenization_spaces=False)]
    if len(answers)!=4*len(valid):raise ModelFailure('semantic_invalid_output')
    reports=[]
    missing=lambda v:v in ('未知','无法确定','无法判断','无法识别','Unknown','unknown','N/A','')
    for index,(item,frame,_) in enumerate(valid):
        fields=answers[4*index:4*index+4]
        data={'subject':'' if missing(fields[0]) else fields[0],'form':'' if missing(fields[1]) else fields[1],
            'keywords_zh':[] if missing(fields[2]) else re.split('[,，、;；\n]+',fields[2]),
            'keywords_en':[] if missing(fields[3]) else re.split('[,，;；\n]+',fields[3]),'uncertainties':[]}
        text=json.dumps(data,ensure_ascii=False);caption,changes=parse_caption(text)
        report={'kind':'visual_fields','model_revision':registry.revision,'upstream_revision':registry.manifest['revision'],
            'prompt_version':PROMPT_VERSION,'source_sha256':frame.source_sha256,'frame':frame.model_dump(mode='json'),
            'caption':caption.model_dump(mode='json'),'raw_text':text,'raw_answers':fields,'normalizations':changes,
            'elapsed_seconds':generated-started,'amortized_inference_seconds':(generated-started)/len(valid),
            'peak_gpu_allocated_bytes':torch.cuda.max_memory_allocated(),'medical_confirmation':False,'effective_values_written':False,
            'full_video_observed':False,'synonym_mapping_published':False,'calibration_state':'not_calibrated',
            'microbatch_size':len(valid),'inference_batch_id':request.get('inference_batch_id'),
            'processor_budget_policy':'size-v2','processed_pixels':processed_pixels[4*index:4*index+4],'decoding_policy':'three-complete-phrases-v1','patch_projection':projection,
            'phase_seconds':{'imports_and_verify':verified-started,'processor_and_model_load':loaded-verified,
                'prepare_inputs':prepared-loaded,'generate':generated-prepared},'generated_tokens':int(tokens.shape[1])}
        reports.append({'task_id':item.get('task_id'),'report':report})
    if diagnostic_path is not None:Path(diagnostic_path).write_text(json.dumps([r['report']['raw_answers'] for r in reports],ensure_ascii=False),encoding='utf8')
    return reports
