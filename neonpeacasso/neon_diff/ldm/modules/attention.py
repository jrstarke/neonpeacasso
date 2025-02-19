import math
import sys
from inspect import isfunction

import torch
import torch.nn.functional as F
from einops import rearrange
from torch import nn, einsum

from ldm.modules.diffusionmodules.util import checkpoint


def exists(val):
    return val is not None


def uniq(arr):
    return {el: True for el in arr}.keys()


def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d


def max_neg_value(t):
    return -torch.finfo(t.dtype).max


def init_(tensor):
    dim = tensor.shape[-1]
    std = 1 / math.sqrt(dim)
    tensor.uniform_(-std, std)
    return tensor


# feedforward
class GEGLU(nn.Module):
    def __init__(self, dim_in, dim_out):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out * 2)

    def forward(self, x):
        x, gate = self.proj(x).chunk(2, dim=-1)
        return x * F.gelu(gate)


class FeedForward(nn.Module):
    def __init__(self, dim, dim_out=None, mult=4, glu=False, dropout=0.):
        super().__init__()
        inner_dim = int(dim * mult)
        dim_out = default(dim_out, dim)
        project_in = nn.Sequential(
            nn.Linear(dim, inner_dim),
            nn.GELU()
        ) if not glu else GEGLU(dim, inner_dim)

        self.net = nn.Sequential(
            project_in,
            nn.Dropout(dropout),
            nn.Linear(inner_dim, dim_out)
        )

    def forward(self, x):
        return self.net(x)


def zero_module(module):
    """
    Zero out the parameters of a module and return it.
    """
    for p in module.parameters():
        p.detach().zero_()
    return module


def Normalize(in_channels):
    return torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)


class LinearAttention(nn.Module):
    def __init__(self, dim, heads=4, dim_head=32):
        super().__init__()
        self.heads = heads
        hidden_dim = dim_head * heads
        self.to_qkv = nn.Conv2d(dim, hidden_dim * 3, 1, bias=False)
        self.to_out = nn.Conv2d(hidden_dim, dim, 1)

    def forward(self, x):
        b, c, h, w = x.shape
        qkv = self.to_qkv(x)
        q, k, v = rearrange(qkv, 'b (qkv heads c) h w -> qkv b heads c (h w)', heads=self.heads, qkv=3)
        k = k.softmax(dim=-1)
        context = torch.einsum('bhdn,bhen->bhde', k, v)
        out = torch.einsum('bhde,bhdn->bhen', context, q)
        out = rearrange(out, 'b heads c (h w) -> b (heads c) h w', heads=self.heads, h=h, w=w)
        return self.to_out(out)


class SpatialSelfAttention(nn.Module):
    def __init__(self, in_channels):
        super().__init__()
        self.in_channels = in_channels

        self.norm = Normalize(in_channels)
        self.q = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.k = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.v = torch.nn.Conv2d(in_channels,
                                 in_channels,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)
        self.proj_out = torch.nn.Conv2d(in_channels,
                                        in_channels,
                                        kernel_size=1,
                                        stride=1,
                                        padding=0)

    def forward(self, x):
        h_ = x
        h_ = self.norm(h_)
        q = self.q(h_)
        k = self.k(h_)
        v = self.v(h_)

        # compute attention
        b, c, h, w = q.shape
        q = rearrange(q, 'b c h w -> b (h w) c')
        k = rearrange(k, 'b c h w -> b c (h w)')
        w_ = torch.einsum('bij,bjk->bik', q, k)

        w_ = w_ * (int(c) ** (-0.5))
        w_ = torch.nn.functional.softmax(w_, dim=2)

        # attend to values
        v = rearrange(v, 'b c h w -> b c (h w)')
        w_ = rearrange(w_, 'b i j -> b j i')
        h_ = torch.einsum('bij,bjk->bik', v, w_)
        h_ = rearrange(h_, 'b c (h w) -> b c h w', h=h)
        h_ = self.proj_out(h_)

        return x + h_


class CrossAttention(nn.Module):
    def __init__(self, query_dim, superfastmode=True, context_dim=None, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads
        context_dim = default(context_dim, query_dim)

        self.scale = dim_head ** -0.5
        self.heads = heads

        self.to_q = nn.Linear(query_dim, inner_dim, bias=False)
        self.to_k = nn.Linear(context_dim, inner_dim, bias=False)
        self.to_v = nn.Linear(context_dim, inner_dim, bias=False)
        self.superfastmode = superfastmode

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, query_dim),
            nn.Dropout(dropout)
        )
        self.fast_forward = superfastmode
        # self.forward = self.fast_forward if superfastmode else self.slow_forward

    def forward(self, x, speed_mp=None, context=None, mask=None, dtype=None):
        h = self.heads
        device = x.device
        secondary_device = device if (self.fast_forward and sys.platform != "darwin") else torch.device("cpu")  # macs
        dtype = x.dtype if dtype is None else dtype
        x = x.to(dtype)
        q_proj = self.to_q(x)
        context = default(context, x)
        k_proj = self.to_k(context)
        v_proj = self.to_v(context)

        del context, x
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (q_proj, k_proj, v_proj))
        del q_proj, k_proj, v_proj
        if sys.platform != "darwin" and device.type != "cpu" and torch.cuda.is_available():  # means we can't count gpu memory
            torch.cuda.empty_cache()
            stats = torch.cuda.memory_stats(device)
            mem_active = stats['active_bytes.all.current']
            mem_reserved = stats['reserved_bytes.all.current']
            mem_free_cuda, _ = torch.cuda.mem_get_info(torch.cuda.current_device())
            mem_free_torch = mem_reserved - mem_active
            mem_free_total = mem_free_cuda + mem_free_torch

            dtype_multiplyer = 2 if str(dtype) == "torch.float16" else 4
            s1, s2, s3, s4 = (q.shape[0] * q.shape[1] * q.shape[1] * 1.5 * dtype_multiplyer), \
                             (q.shape[0] * (q.shape[1] ** 2) * dtype_multiplyer), \
                             (q.shape[0] * q.shape[1] * q.shape[2] * 3 * dtype_multiplyer), \
                             (q.shape[0] * q.shape[1] * v.shape[2] * 2 * dtype_multiplyer)
            s = int((s1 + s2 + s3 + s4))
            # 4 main operations' needed compute memory: softmax, einsum, another einsum, and r1 allocation memory.
            speed_mp = (2 if self.fast_forward else 3) if speed_mp is None else speed_mp
            chunk_split = int(((s // mem_free_total) + 1) * 2 * speed_mp) if s > mem_free_total else 1
        else:
            chunk_split = 2 if speed_mp is None else speed_mp  # :D

        r1 = torch.zeros(q.shape[0], q.shape[1], v.shape[2], device=secondary_device)
        mp = q.shape[1] // chunk_split
        # print("The operation will need \t", s, s // 1024 // 1024)
        # print("The available memory is \t", mem_free_total, mem_free_total // 1024 // 1024)
        # print(f"Splitting into {chunk_split} chunks")
        for i in range(0, q.shape[1], mp):
            q, k = q.to(device), k.to(device)
            s1 = einsum('b i d, b j d -> b i j', q[:, i:i + mp], k)
            q, k = q.to(secondary_device), k.to(secondary_device)
            s1 *= self.scale
            s1 = F.softmax(s1, dim=-1)
            r1[:, i:i + mp] = einsum('b i j, b j d -> b i d', s1, v).to(secondary_device)
        r1 = rearrange(r1, '(b h) n d -> b n (h d)', h=h).to(device)
        return self.to_out(r1)


class BasicTransformerBlock(nn.Module):
    def __init__(self, dim, n_heads, d_head, dropout=0., superfastmode=True, context_dim=None, gated_ff=True,
                 checkpoint=True):
        super().__init__()
        self.attn1 = CrossAttention(query_dim=dim, heads=n_heads, dim_head=d_head,
                                    dropout=dropout, superfastmode=superfastmode)  # is a self-attention
        self.ff = FeedForward(dim, dropout=dropout, glu=gated_ff)
        self.attn2 = CrossAttention(query_dim=dim, context_dim=context_dim,
                                    heads=n_heads, dim_head=d_head, dropout=dropout, superfastmode=superfastmode)
        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.norm3 = nn.LayerNorm(dim)
        self.checkpoint = checkpoint

    def forward(self, x, speed_mp=None, context=None):
        return checkpoint(self._forward, (x, speed_mp, context), self.parameters(), self.checkpoint)

    def _forward(self, x, speed_mp=None, context=None):
        x = self.attn1(self.norm1(x), speed_mp=speed_mp, dtype=x.dtype) + x
        x = self.attn2(self.norm2(x), speed_mp=speed_mp, context=context, dtype=x.dtype) + x
        x = self.ff(self.norm3(x)) + x
        return x


class SpatialTransformer(nn.Module):
    """
    Transformer block for image-like data.
    First, project the input (aka embedding)
    and reshape to b, t, d.
    Then apply standard transformer action.
    Finally, reshape to image
    """

    def __init__(self, in_channels, n_heads, d_head,
                 depth=1, dropout=0., superfastmode=True, context_dim=None):
        super().__init__()
        self.in_channels = in_channels
        inner_dim = n_heads * d_head
        self.norm = Normalize(in_channels)

        self.proj_in = nn.Conv2d(in_channels,
                                 inner_dim,
                                 kernel_size=1,
                                 stride=1,
                                 padding=0)

        self.transformer_blocks = nn.ModuleList(
            [BasicTransformerBlock(inner_dim, n_heads, d_head, superfastmode=superfastmode, dropout=dropout,
                                   context_dim=context_dim)
             for _ in range(depth)]
        )
        self.proj_out = zero_module(nn.Conv2d(inner_dim,
                                              in_channels,
                                              kernel_size=1,
                                              stride=1,
                                              padding=0))

    def forward(self, x, context=None, speed_mp=None):
        # note: if no context is given, cross-attention defaults to self-attention
        b, c, h, w = x.shape
        x_in = x
        x = self.norm(x)
        x = self.proj_in(x)
        x = rearrange(x, 'b c h w -> b (h w) c')
        for block in self.transformer_blocks:
            x = block(x, speed_mp=speed_mp, context=context)
        x = rearrange(x, 'b (h w) c -> b c h w', h=h, w=w)
        x = self.proj_out(x)
        return x + x_in
