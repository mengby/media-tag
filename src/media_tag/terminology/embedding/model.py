"""Actual CPU BGE encoding, using the upstream CLS/L2 recipe and no remote code."""
from time import monotonic
from media_tag.models.contracts import ModelFailure


class BGEModel:
    def __init__(self,registry):
        from media_tag.models.native import prepare_native_runtime
        prepare_native_runtime()
        paths=registry.verify()
        import torch
        from transformers import BertModel,BertTokenizerFast
        torch.set_num_threads(2)
        self.torch=torch;self.registry=registry
        self.tokenizer=BertTokenizerFast.from_pretrained(paths['config.json'].parent,local_files_only=True)
        self.model=BertModel.from_pretrained(paths['config.json'].parent,local_files_only=True,use_safetensors=True,
                                           attn_implementation='eager').eval().to('cpu')
        if self.model.config.hidden_size!=512 or self.model.config.max_position_embeddings!=512:
            raise ModelFailure('text_model_shape')

    def encode(self,texts,*,query=False):
        if not isinstance(texts,list) or not 1<=len(texts)<=16 or any(not isinstance(t,str) or not t.strip() or len(t)>8000 for t in texts):
            raise ModelFailure('text_input_budget')
        values=[self.registry.manifest['query_instruction']+t if query else t for t in texts]
        encoded=self.tokenizer(values,padding=True,truncation=False,return_tensors='pt')
        # Never silently truncate away a negation, side, or direction at the end.
        if encoded['input_ids'].shape[1]>512:raise ModelFailure('text_token_budget')
        with self.torch.inference_mode():
            vectors=self.model(**encoded).last_hidden_state[:,0,:].float()
            vectors=self.torch.nn.functional.normalize(vectors,p=2,dim=1)
        if not self.torch.isfinite(vectors).all():raise ModelFailure('text_model_nonfinite')
        return vectors.cpu().tolist()
