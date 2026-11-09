"""Qwen3-VL pixel budget: set the size actually consumed by Transformers 4.57."""


def make_processor(root,max_pixels):
    from transformers import AutoProcessor
    processor=AutoProcessor.from_pretrained(root,local_files_only=True,trust_remote_code=False,use_fast=False,
        min_pixels=128**2,max_pixels=max_pixels)
    processor.image_processor.size={'shortest_edge':128**2,'longest_edge':max_pixels}
    processor.tokenizer.padding_side='left'
    return processor
