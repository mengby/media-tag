"""Equivalent full-patch projection, avoiding slow BF16 Conv3d dispatch on this Windows GPU."""


def replace_patch_projection(model):
    import torch
    from torch import nn
    from torch.nn import functional as F
    patch=model.model.visual.patch_embed
    original=patch.proj
    if not isinstance(original,nn.Conv3d) or original.groups!=1 or original.padding!=(0,0,0) or original.dilation!=(1,1,1) or original.stride!=original.kernel_size:
        raise ValueError('unsupported vision patch projection')
    expected=original.kernel_size
    class FullPatchLinear(nn.Module):
        def __init__(self):
            super().__init__();self.weight=original.weight;self.bias=original.bias
        def forward(self,value):
            if value.ndim!=5 or tuple(value.shape[2:])!=expected:raise ValueError('patch must exactly cover the convolution kernel')
            result=F.linear(value.flatten(1),self.weight.flatten(1),self.bias)
            return result[:,:,None,None,None]
    patch.proj=FullPatchLinear()
    return 'full-patch-linear-v1'
