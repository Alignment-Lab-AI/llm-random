import torch
import torch.nn.functional as F
from fancy_einsum import einsum

from lizrd.core import nn
from lizrd.core.misc import get_init_weight
from lizrd.support import ash
from research.conditional.utils.layer_manager import LoggingLayer


class ExpertChoiceFF(LoggingLayer):
    def __init__(
        self, dmodel: int, n_experts: int, expert_size: int, topk_perc: float
    ):
        """
        Args:
            dmodel: dimension of the input
            n_experts: number of experts
            expert_size: size of each expert
            topk: number of tokens that will be chosen for each expert
        """
        super().__init__()

        self.dmodel = dmodel
        self.n_experts = n_experts
        self.expert_size = expert_size
        self.topk_perc = topk_perc

        self.lin1_weight = nn.Parameter(
            get_init_weight((n_experts, dmodel, expert_size), fan_in=dmodel)
        )
        self.lin2_weight = nn.Parameter(
            get_init_weight((n_experts, expert_size, dmodel), fan_in=expert_size)
        )
        self.gate = nn.Parameter(
            get_init_weight((dmodel, n_experts), fan_in=dmodel)
        ).requires_grad_(True)
        self.old_gate = self.gate.data.clone()

    def forward(self, x: torch.Tensor):
        x_in = x.clone()
        self.old_gate = self.old_gate.to(self.gate.device)
        # check if values in gate are approx. the same as before
        if torch.allclose(self.gate, self.old_gate):
            print("gate is same")
        else:
            print("gate is different")
        # x is (batch, cutoff, dmodel)
        batch_size, cutoff = x.shape[0], x.shape[1]
        n_examples = batch_size * cutoff
        topk = round(self.topk_perc / 100 * n_examples)

        # expert embedding
        gate_out = einsum(
            "batch_size cutoff dmodel, dmodel n_experts -> batch_size cutoff n_experts",
            x,
            self.gate,
        )
        # transform such that first dimension corresponds to experts
        gate_out = gate_out.permute(2, 0, 1)
        # flatten batch_size x cutoff
        gate_out = gate_out.flatten(start_dim=1)
        # perform softmax over tokens for each expert
        gate_out = torch.softmax(gate_out, dim=1)
        # choose topk tokens for each expert
        topk_values, topk_indices = torch.topk(gate_out, k=topk, dim=1)

        # flatten x s. t. first dimension is tokens instead of batch_size x cutoff
        x = x.flatten(start_dim=0, end_dim=1)

        # save x
        x_before_ff = x

        # choose the right tokens from x for each expert
        x = torch.index_select(x, dim=0, index=topk_indices.flatten()).reshape(
            (self.n_experts, topk, self.dmodel)
        )
        x = x.reshape((self.n_experts, topk, self.dmodel))

        # feed through ff
        # lin1 maps from (n_experts, topk, dmodel) to (n_experts, topk, exp_size)
        # x = einsum(
        #     "n_exp topk dmodel, n_exp dmodel exp_size -> n_exp topk exp_size",
        #     x,
        #     self.lin1_weight,
        # )

        # x = F.relu(x)

        # # lin2 maps from (n_experts, topk, exp_size) to (n_experts, topk, dmodel)
        # x = einsum(
        #     "n_exp topk exp_size, n_exp exp_size dmodel -> n_exp topk dmodel",
        #     x,
        #     self.lin2_weight,
        # )
        ash.assert_shape("e k m", x, e=self.n_experts, k=topk, m=self.dmodel)

        # multiply by softmax
        # ash.assert_shape("e k", topk_values, e=self.n_experts, k=topk)
        # x = einsum("n_exp topk dmodel, n_exp topk -> n_exp topk dmodel", x, topk_values)

        # flatten x s. t. first dimension is tokens instead of n_experts x topk
        x = x.flatten(start_dim=0, end_dim=1)

        # add tokens that have been processed by more than one expert
        z = torch.zeros_like(x_before_ff).type(x.type())
        z.index_add_(dim=0, index=topk_indices.flatten().to(int), source=x)

        # reshape to (batch_size, cutoff, dmodel)
        x = z.reshape((batch_size, cutoff, self.dmodel))

        assert torch.isclose(x_in, x).all()

        return x

    def log_light(self):
        return dict()

    def log_heavy(self):
        return dict()
