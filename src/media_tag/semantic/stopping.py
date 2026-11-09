"""Stop field rows at complete phrases, rather than generating repetitive keyword lists."""
import re
from .contracts import FORMS


def complete(field,text):
    text=text.strip()
    if text in ('未知','无法判断','无法确定','unknown','Unknown'):return True
    if field==1:return text in FORMS
    if field==0:return '\n' in text or '。' in text
    parts=re.split('[,，、;；\n]',text)
    # The final part may still be an unfinished word. Count only delimiter-closed phrases.
    words=[v.strip() for v in parts[:-1] if v.strip()]
    return len(set(words))>=3


def criteria(processor,prompt_length):
    import torch
    from transformers import StoppingCriteria,StoppingCriteriaList
    class FieldsComplete(StoppingCriteria):
        def __call__(self,input_ids,scores,**kwargs):
            texts=processor.batch_decode(input_ids[:,prompt_length:],skip_special_tokens=True,clean_up_tokenization_spaces=False)
            return torch.tensor([complete(i%4,text) for i,text in enumerate(texts)],device=input_ids.device,dtype=torch.bool)
    return StoppingCriteriaList([FieldsComplete()])
