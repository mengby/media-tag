"""Opt-in local inference; importing this module never imports torch or loads weights."""
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from media_tag.semantic.registry import SemanticRegistry

from .contracts import Generation
from .prompt import SYSTEM, PROMPT_VERSION


class LocalQwenGenerator:
    def __init__(self, model_root, *, enabled=False):
        if type(enabled) is not bool:
            raise ValueError('explicit_boolean_execution_flag_required')
        self.registry = SemanticRegistry(Path(model_root))
        self.enabled = enabled

    @property
    def model_revision(self):
        return self.registry.revision

    def readiness(self):
        """No file hashing, native import, GPU probe or model execution."""
        return {'enabled': self.enabled, 'model': 'Qwen/Qwen3-VL-2B-Instruct',
                'model_revision': self.model_revision, 'prompt_version': PROMPT_VERSION,
                'requirements': ['pinned local weights verification at invocation', 'exclusive GPU scheduling',
                                 'CUDA with bfloat16', 'caller-owned bounded worker timeout/process tree'],
                'model_executed': False, 'business_validation': 'pending'}

    def __call__(self, *, image_bytes, field_prompts, options):
        if not self.enabled:
            raise RuntimeError('local_topic_native_execution_not_enabled')
        if len(field_prompts) != 10 or any(len(p) > 14000 for p in field_prompts):
            raise ValueError('prompt_budget')
        from media_tag.models.native import prepare_native_runtime
        prepare_native_runtime()
        from PIL import Image
        import torch
        from transformers import Qwen3VLForConditionalGeneration
        from media_tag.semantic.processor import make_processor
        from media_tag.semantic.patch_projection import replace_patch_projection

        self.registry.verify()
        if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
            raise RuntimeError('local_topic_requires_cuda_bfloat16')
        with Image.open(BytesIO(image_bytes)) as source:
            image = source.convert('RGB')
        torch.set_num_threads(1)
        torch.manual_seed(0)
        torch.cuda.set_per_process_memory_fraction(.75)
        processor = make_processor(self.registry.root, options.max_pixels)
        model = Qwen3VLForConditionalGeneration.from_pretrained(self.registry.root,
            local_files_only=True, trust_remote_code=False, dtype=torch.bfloat16,
            attn_implementation='sdpa', device_map={'': 'cuda:0'}, low_cpu_mem_usage=True).eval()
        replace_patch_projection(model)
        answers = []
        try:
            # One field at a time: ten independent tasks do not become a ten-image GPU batch.
            for prompt in field_prompts:
                messages = [{'role': 'system', 'content': SYSTEM}, {'role': 'user', 'content': [
                    {'type': 'image', 'image': image}, {'type': 'text', 'text': prompt}]}]
                inputs = processor.apply_chat_template(messages, add_generation_prompt=True, tokenize=True,
                    return_dict=True, return_tensors='pt').to('cuda')
                pixels = sum(int(row[1])*int(row[2])*16**2 for row in inputs['image_grid_thw'])
                if pixels > options.max_pixels or inputs['input_ids'].shape[1] > 6000:
                    raise ValueError('local_topic_processor_budget')
                with torch.inference_mode():
                    generated = model.generate(**inputs, max_new_tokens=options.max_new_tokens, do_sample=False)
                answers.append(processor.batch_decode(generated[:, inputs['input_ids'].shape[1]:],
                    skip_special_tokens=True, clean_up_tokenization_spaces=False)[0])
                del generated, inputs
            torch.cuda.synchronize()
            return Generation(backend='local_qwen', model_revision=self.model_revision,
                prompt_version=PROMPT_VERSION, image_sha256=sha256(image_bytes).hexdigest(), answers=tuple(answers))
        finally:
            del model, processor
            torch.cuda.empty_cache()
