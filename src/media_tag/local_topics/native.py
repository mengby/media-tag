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
        return self.generate_batch(images=(image_bytes,), field_prompts=field_prompts, options=options)[0]

    def generate_batch(self, *, images, field_prompts, options, field_rules=None, microbatch_size=4,
                       deadline=None, read_input=None, yield_requested=None):
        import time
        from .parser import parse_answer
        if not self.enabled:
            raise RuntimeError('local_topic_native_execution_not_enabled')
        if not 1 <= len(images) <= 32 or not 1 <= microbatch_size <= 4:
            raise ValueError('local_topic_batch_budget')
        if not 1 <= len(field_prompts) <= 42 or any(len(p) > 14000 for p in field_prompts):
            raise ValueError('prompt_budget')
        if field_rules is not None and len(field_rules) != len(field_prompts):
            raise ValueError('local_topic_field_binding')
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
        torch.set_num_threads(1)
        torch.manual_seed(0)
        torch.cuda.set_per_process_memory_fraction(.75)
        processor = make_processor(self.registry.root, options.max_pixels)
        if len(images) > 1:
            processor.tokenizer.padding_side = 'left'
        model = Qwen3VLForConditionalGeneration.from_pretrained(self.registry.root,
            local_files_only=True, trust_remote_code=False, dtype=torch.bfloat16,
            attn_implementation='sdpa', device_map={'': 'cuda:0'}, low_cpu_mem_usage=True).eval()
        replace_patch_projection(model)
        def infer_field(group, prompt):
            conversations = [[{'role': 'system', 'content': [{'type': 'text', 'text': SYSTEM}]},
                              {'role': 'user', 'content': [{'type': 'image', 'image': image},
                                                            {'type': 'text', 'text': prompt}]}]
                             for image in group]
            inputs = processor.apply_chat_template(
                conversations[0] if len(group) == 1 else conversations, padding=len(group) > 1,
                add_generation_prompt=True, tokenize=True, return_dict=True, return_tensors='pt').to('cuda')
            pixels = [int(row[1])*int(row[2])*16**2 for row in inputs['image_grid_thw']]
            if len(pixels) != len(group) or max(pixels) > options.max_pixels or inputs['input_ids'].shape[1] > 6000:
                raise ValueError('local_topic_processor_budget')
            with torch.inference_mode():
                generated = model.generate(**inputs, max_new_tokens=options.max_new_tokens, do_sample=False)
            return processor.batch_decode(generated[:, inputs['input_ids'].shape[1]:],
                skip_special_tokens=True, clean_up_tokenization_spaces=False)

        results = []
        width = microbatch_size
        last_group_seconds = 0.
        try:
            for start in range(0, len(images), microbatch_size):
                began = time.monotonic()
                if start and ((deadline is not None and deadline - began < 1.5 * last_group_seconds + 2)
                              or (yield_requested and yield_requested())):
                    break
                group_items = images[start:start + microbatch_size]
                raw_images = [read_input(item) for item in group_items] if read_input else group_items
                pictures = []
                for raw in raw_images:
                    with Image.open(BytesIO(raw)) as source:
                        pictures.append(source.convert('RGB'))
                answers = [[] for _ in pictures]
                retried = [{} for _ in pictures]
                for field_index, prompt in enumerate(field_prompts):
                    offset = 0
                    while offset < len(pictures):
                        group = pictures[offset:offset + width]
                        try:
                            decoded = infer_field(group, prompt)
                        except torch.OutOfMemoryError:
                            if len(group) == 1:
                                raise
                            width = max(1, len(group) // 2)
                        else:
                            if len(decoded) != len(group):
                                raise ValueError('local_topic_generation_count')
                            for i, answer in enumerate(decoded, offset):
                                if len(group) > 1 and field_rules:
                                    rule = field_rules[field_index]
                                    try:
                                        parse_answer(answer, *rule)
                                    except (ValueError, TypeError, KeyError):
                                        retried[i][rule[0]] = answer[:16000]
                                        answer = infer_field([pictures[i]], prompt)[0]
                                answers[i].append(answer)
                            offset += len(group)
                            continue
                        # Leave the exception scope so the failed tensors are freed first.
                        torch.cuda.empty_cache()
                torch.cuda.synchronize()
                results.extend(Generation(backend='local_qwen', model_revision=self.model_revision,
                    prompt_version=PROMPT_VERSION, image_sha256=sha256(raw).hexdigest(), answers=tuple(row),
                    retried_answers=originals) for raw, row, originals in zip(raw_images, answers, retried, strict=True))
                last_group_seconds = time.monotonic() - began
            return results
        finally:
            del model, processor
            torch.cuda.empty_cache()
