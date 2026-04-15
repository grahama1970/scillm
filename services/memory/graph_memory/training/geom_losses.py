from __future__ import annotations
"""
Tiny geometry-aware contrastive loss for sentence-transformers style training.

L = InfoNCE(a,p,N) + λL * Laplacian + λD * Distillation

This module is intentionally minimal. Integrate into your own training loop
or import into a sentence-transformers custom loss wrapper.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
from typing import Optional


class GeomContrastiveLoss(nn.Module):
    def __init__(self, tau: float = 0.07, lambda_lap: float = 0.2, lambda_distill: float = 0.1, lambda_align: float = 0.05, lambda_align_param: float = 0.04):
        super().__init__()
        self.tau = tau
        self.lambda_lap = lambda_lap
        self.lambda_distill = lambda_distill
        self.lambda_align = lambda_align
        self.lambda_align_param = lambda_align_param

    def _nce(self, a: torch.Tensor, p: torch.Tensor, negs: Optional[torch.Tensor]):
        a = F.normalize(a, dim=-1)
        p = F.normalize(p, dim=-1)
        logits_pos = (a * p).sum(dim=-1, keepdim=True) / self.tau
        if negs is None or negs.numel() == 0:
            logits = logits_pos
            labels = torch.zeros(a.size(0), dtype=torch.long, device=a.device)
            return F.cross_entropy(logits, labels)
        negs = F.normalize(negs, dim=-1)  # (B,K,D)
        logits_neg = torch.einsum("bd,bkd->bk", a, negs) / self.tau
        logits = torch.cat([logits_pos, logits_neg], dim=1)
        labels = torch.zeros(a.size(0), dtype=torch.long, device=a.device)
        return F.cross_entropy(logits, labels)

    def _laplacian(self, z: torch.Tensor, edge_index: Optional[torch.Tensor], edge_weight: Optional[torch.Tensor]):
        if edge_index is None or edge_index.numel() == 0:
            return z.new_zeros(())
        i, j = edge_index[0], edge_index[1]
        dif = z[i] - z[j]
        if edge_weight is not None:
            dif = dif * edge_weight.view(-1, 1)
        return (dif.pow(2).sum(dim=-1)).mean()

    def _distill(self, z: torch.Tensor, z_teacher: Optional[torch.Tensor]):
        if z_teacher is None:
            return z.new_zeros(())
        z = F.normalize(z, dim=-1)
        zt = F.normalize(z_teacher, dim=-1)
        return (1.0 - (z * zt).sum(dim=-1)).mean()

    def forward(
        self,
        anchor: torch.Tensor,
        positive: torch.Tensor,
        negatives: Optional[torch.Tensor] = None,
        edge_index: Optional[torch.Tensor] = None,
        edge_weight: Optional[torch.Tensor] = None,
        anchor_teacher: Optional[torch.Tensor] = None,
        positive_teacher: Optional[torch.Tensor] = None,
        pos_weights: Optional[torch.Tensor] = None,
        q_goal: Optional[torch.Tensor] = None,
        align_mask: Optional[torch.Tensor] = None,
        q_param: Optional[torch.Tensor] = None,
        align_param_mask: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        z = torch.cat([anchor, positive], dim=0)
        # InfoNCE (optionally weight positives)
        if pos_weights is None:
            nce = self._nce(anchor, positive, negatives)
        else:
            # Implement weighted NCE by expanding weights into loss
            a = F.normalize(anchor, dim=-1)
            p = F.normalize(positive, dim=-1)
            logits_pos = (a * p).sum(dim=-1, keepdim=True) / self.tau
            if negatives is None or negatives.numel() == 0:
                logits = logits_pos
                labels = torch.zeros(a.size(0), dtype=torch.long, device=a.device)
                nce_all = F.cross_entropy(logits, labels, reduction='none')
                w = pos_weights.view(-1).clamp_min(1e-8)
                nce = (nce_all * w).sum() / w.sum()
            else:
                negs = F.normalize(negatives, dim=-1)
                logits_neg = torch.einsum("bd,bkd->bk", a, negs) / self.tau
                logits = torch.cat([logits_pos, logits_neg], dim=1)
                labels = torch.zeros(a.size(0), dtype=torch.long, device=a.device)
                nce_all = F.cross_entropy(logits, labels, reduction='none')
                w = pos_weights.view(-1).clamp_min(1e-8)
                nce = (nce_all * w).sum() / w.sum()
        lap = self._laplacian(z, edge_index, edge_weight)
        dist = 0.5 * (
            self._distill(anchor, anchor_teacher) + self._distill(positive, positive_teacher)
        ) if self.lambda_distill > 0 else z.new_zeros(())
        loss = nce + self.lambda_lap * lap + self.lambda_distill * dist
        # Optional NL↔Lean goal alignment (pull anchor toward goal embedding)
        if self.lambda_align > 0.0 and q_goal is not None and align_mask is not None:
            mask = align_mask.bool().view(-1)
            if mask.any():
                qn = F.normalize(anchor[mask], dim=-1)
                gn = F.normalize(q_goal[mask], dim=-1)
                align = 1.0 - (qn * gn).sum(dim=-1)
                loss = loss + self.lambda_align * align.mean()
        # Optional NL↔Param alignment
        if self.lambda_align_param > 0.0 and q_param is not None and align_param_mask is not None:
            pmask = align_param_mask.bool().view(-1)
            if pmask.any():
                qn = F.normalize(anchor[pmask], dim=-1)
                pn = F.normalize(q_param[pmask], dim=-1)
                palign = 1.0 - (qn * pn).sum(dim=-1)
                loss = loss + self.lambda_align_param * palign.mean()
        return loss
