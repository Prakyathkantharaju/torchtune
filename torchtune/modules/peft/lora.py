# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.
import math
from typing import List

import torch
import torch.nn.functional as F

from torch import nn, Tensor

from torchao.dtypes.nf4tensor import linear_nf4, to_nf4
from torchtune.modules.peft.peft_utils import AdapterModule
from torchtune.utils import _register_nf4_dispatch_ops  # noqa: F401


class LoRALinear(nn.Module, AdapterModule):
    """LoRA linear layer as introduced in `LoRA: Low-Rank Adaptation of Large Language Models <https://arxiv.org/abs/2106.09685>`_.

    LoRA perturbs a given layer via a low-rank approximation where only
    the rank decomposition matrices are trainable. In a linear layer instead of
    :math:`x \\mapsto W_0x` a LoRALinear layer is defined as
    :math:`x \\mapsto W_0x + (\\alpha / r)BAx`, where :math:`r` is the rank of
    the matrices :math:`A` and :math:`B` and :math:`\\alpha` is a scaling factor.
    As in the original implementation, we support dropout before multiplication
    by the low-rank matrices.

    Args:
        in_dim (int): input dimension
        out_dim (int): output dimension
        rank (int): rank of the low-rank approximation
        alpha (float): scaling factor for the low-rank approximation
        dropout (float): dropout probability. Default: 0.0
        use_dora (bool): whether to use DORA (weight-Decomposed Low-Rank Adaptation).
            link to the paper: https://arxiv.org/pdf/2402.09353
            Default: False
        use_bias (bool): whether to include bias in the original linear layer.
            Default: False
        quantize_base (bool): Whether to quantize base linear weight or not.
            Default: False
    """

    def __init__(
        self,
        in_dim: int,
        out_dim: int,
        rank: int,
        alpha: float,
        dropout: float = 0.0,
        use_dora: bool = False,  # TODO(prakyath): add this at each models inference, Do Not make this aas default True.
        use_bias: bool = False,
        quantize_base: bool = False,
    ):
        super().__init__()
        self.in_dim = in_dim
        self.rank = rank
        self.alpha = alpha
        self.out_dim = out_dim
        self.use_bias = use_bias
        self.use_dora = use_dora
        self._quantize_base = quantize_base
        weight, bias = self._create_weight_and_bias()
        # 'self.disabled' is a flag showing whether to turn off LoRA adapters,
        # this can be used in DPO for treating the lora adapters as the policy model
        # and disabling it to treat the base model as the reference model
        self.disabled = False
        self.register_parameter("weight", nn.Parameter(weight))
        self.register_parameter(
            "bias", nn.Parameter(bias) if bias is not None else None
        )
        self.dropout = nn.Dropout(p=dropout)
        self.lora_a = nn.Linear(in_features=in_dim, out_features=rank, bias=False)
        self.lora_b = nn.Linear(in_features=rank, out_features=out_dim, bias=False)
        if self.use_dora:
            self.lora_m = nn.Parameter(torch.zeros(1, out_dim))
        # Note: FSDP's meta device initialization contract assumes that a module's
        # reset_parameters method only initializes its own parameters (i.e. no child
        # params are initialized, as is done in initialize_parameters below).
        # For that reason, we patch reset_parameters directly on lora_a and lora_b submodules
        # when using meta device. This is done in
        # torchtune.utils.prepare_model_for_fsdp_with_meta_device.
        # See this issue for more details: https://github.com/pytorch/pytorch/issues/104187.
        # Without meta device, we only need the following:
        self.initialize_parameters()

    def initialize_parameters(self):
        # Initialize as in
        # https://github.com/microsoft/LoRA/blob/4c0333854cb905966f8cc4e9a74068c1e507c7b7/loralib/layers.py#L119
        _lora_a_init_params(self.lora_a)
        _lora_b_init_params(self.lora_b)
        if self.use_dora:
            _dora_m_init_params(self.lora_m)

    def _create_weight_and_bias(self):
        """
        Creates a linear weight and bias tensor, using NF4 dtype if we're quantizing
        (indicated via quantize_base=True).
        """
        in_dim, out_dim, use_bias = self.in_dim, self.out_dim, self.use_bias
        linear = nn.Linear(in_features=in_dim, out_features=out_dim, bias=use_bias)
        weight = linear.weight if not self._quantize_base else to_nf4(linear.weight)
        bias = None
        if self.use_bias:
            if self._quantize_base:
                raise NotImplementedError(
                    "Quantized LoRALinear does not support bias at the moment."
                )
            bias = linear.bias
        return weight, bias

    def adapter_params(self) -> List[str]:
        """
        Return lora_a.weight and lora_b.weight as adapter params.
        If bias is enabled, also return lora_a.bias and lora_b.bias.
        """
        # NOTE: this function has to be updated if the names of "lora_a" and "lora_b"
        # in this module change.
        adapter_params = ["lora_a.weight", "lora_b.weight"]
        if self.use_dora:
            adapter_params.append("lora_m")
        return adapter_params

    def init_dora(self) -> None:
        # this is a seperate function because,
        # this should be called after model state dict is called.
        # But We verify and initialize the model arch first before the loading weights.
        weight_norm = self._dora_weight_norm
        self.lora_m.data = weight_norm.data  # Update the data of 'm' directly

    @property
    def _dora_weight_norm(self) -> Tensor:
        """
        Compute the norm of the linear weight and lora adaptor weights.
        If the base model is quantized, dequantize the weights before computing the norm.
        Return the norm in NF4 format if the base model is quantized.
        """
        weight = self.weight.dequantize() if self._quantize_base else self.weight

        # Perform the operation with regular tensors
        result = weight + (self.alpha / self.rank) * (
            self.lora_b.weight @ self.lora_a.weight
        )
        norm = torch.linalg.norm(result, dim=1)

        # Clamp the norm to avoid division by zero
        # TODO(Prakyath): Check with torchtune team whether this should be a parameter ?
        norm = torch.clamp(norm, min=1e-6)
        # Return the norm in NF4 format.
        return to_nf4(norm) if self._quantize_base else norm

    def forward(self, x: Tensor) -> Tensor:
        """
        Args:
            x (Tensor): input tensor with shape ``(..., in_dim)``

        Returns:
            Tensor: output tensor with shape ``(..., out_dim)``

        """
        if self._quantize_base:
            out = linear_nf4(input=x, weight=self.weight)
        else:
            out = F.linear(x, self.weight, self.bias)
        if self.disabled:
            return out
        lora_out = self.lora_a(self.dropout(x))
        lora_out = (self.alpha / self.rank) * self.lora_b(lora_out)
        # Author mentions this method is faster for the computation purpose:
        # https://github.com/huggingface/peft/pull/1474#issuecomment-1963402710
        if self.use_dora:
            weight_norm = self._dora_weight_norm.detach()
            mag_norm_scale = (self.lora_m / weight_norm).view(1, -1)
            # PEFT uses: out + (mag_norm_scale - 1) * out  + mag_norm_scale * lora_b(lora_a(x)) * scaling.
            return (out + lora_out) * mag_norm_scale
        return out + lora_out


def _lora_a_init_params(x: nn.Linear) -> None:
    """
    Initialize LoRA A weight to Kaiming uniform.
    """
    nn.init.kaiming_uniform_(x.weight, a=math.sqrt(5))


def _lora_b_init_params(x: nn.Linear) -> None:
    """
    Initialize LoRA B weight to zeros.
    """
    nn.init.zeros_(x.weight)


def _dora_m_init_params(x: nn.Parameter) -> None:
    """
    Initialize DORA m to ones.
    """
    nn.init.ones_(x)
