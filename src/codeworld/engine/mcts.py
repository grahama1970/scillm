from __future__ import annotations
"""
Deterministic MCTS engine for CodeWorld (Phase 1).

Safety: does not execute user code; uses a deterministic pseudo-reward based on
(task, variant_key, depth_step, seed). Unvisited children receive +inf UCT to
guarantee initial exploration.

Seed precedence:
 - explicit seed param
 - env SCILLM_DETERMINISTIC_SEED (int or any string hashed deterministically)
 - None (recorded)
"""

import hashlib
import json
import math
import os
import time
from typing import Any, Dict, List, Optional


def _normalize_seed(seed: Optional[Any]) -> Optional[int]:
    if seed is None:
        env_seed = os.getenv("SCILLM_DETERMINISTIC_SEED")
        if not env_seed:
            return None
        try:
            return int(env_seed)
        except ValueError:
            h = hashlib.sha256(env_seed.encode("utf-8")).hexdigest()
            return int(h[:16], 16)
    if isinstance(seed, int):
        return seed
    if isinstance(seed, str):
        try:
            return int(seed)
        except ValueError:
            h = hashlib.sha256(seed.encode("utf-8")).hexdigest()
            return int(h[:16], 16)
    h = hashlib.sha256(repr(seed).encode("utf-8")).hexdigest()
    return int(h[:16], 16)


def _variant_key(variant: Any) -> str:
    if isinstance(variant, (dict, list, tuple)):
        try:
            return json.dumps(variant, sort_keys=True, ensure_ascii=False)
        except Exception:
            return repr(variant)
    if isinstance(variant, str):
        return variant
    return repr(variant)


def _pseudo_reward(task: Any, variant_key: str, depth_step: int, seed: Optional[int]) -> float:
    material = f"{task}|{variant_key}|{depth_step}|{seed}"
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], "big")
    return (val % (1 << 53)) / float(1 << 53)


def run_mcts(
    task: Any,
    context: Optional[Dict[str, Any]],
    code_variants: List[Any] | Dict[str, Any],
    rollouts: int = 64,
    depth: int = 8,
    uct_c: float = 1.4,
    seed: Optional[Any] = None,
    timeout_ms: int = 50,
) -> Dict[str, Any]:
    start = time.monotonic()
    eff_seed = _normalize_seed(seed)

    # Accept either list of variants or mapping name->code
    variants: List[Any]
    if isinstance(code_variants, dict):
        variants = list(code_variants.values())
    else:
        variants = list(code_variants)

    n = len(variants)
    if n == 0:
        return {
            "best_variant": None,
            "best_value": float("-inf"),
            "rollouts": 0,
            "depth": depth,
            "uct_c": uct_c,
            "visits": 0,
            "explored": 0,
            "seed": eff_seed,
            "error": "no_variants",
            "child_stats": [],
        }

    keys = [_variant_key(v) for v in variants]
    visits = [0] * n
    value_sum = [0.0] * n

    total = 0
    r_idx = 0
    try:
        while r_idx < int(rollouts):
            if timeout_ms > 0 and (time.monotonic() - start) * 1000.0 >= timeout_ms:
                break
            tot_vis = max(1, sum(visits))
            # UCT score with +inf for unvisited
            def uct(i: int) -> float:
                if visits[i] == 0:
                    return float("inf")
                mean = value_sum[i] / visits[i]
                return mean + float(uct_c) * math.sqrt(math.log(tot_vis) / visits[i])

            best_i = max(range(n), key=uct)
            acc = 0.0
            D = max(1, int(depth))
            for d in range(1, D + 1):
                acc += _pseudo_reward(task, keys[best_i], d, eff_seed)
            reward = acc / float(D)
            visits[best_i] += 1
            value_sum[best_i] += reward
            total += 1
            r_idx += 1
    except Exception as e:
        return {
            "best_variant": None,
            "best_value": float("-inf"),
            "rollouts": total,
            "depth": depth,
            "uct_c": uct_c,
            "visits": sum(visits),
            "explored": sum(1 for v in visits if v > 0),
            "seed": eff_seed,
            "error": str(e),
            "child_stats": [
                {
                    "index": i,
                    "visits": visits[i],
                    "value_sum": value_sum[i],
                    "value_mean": (value_sum[i] / visits[i]) if visits[i] else 0.0,
                }
                for i in range(n)
            ],
        }

    best_idx = max(range(n), key=lambda i: (value_sum[i] / visits[i]) if visits[i] else float("-inf"))
    best_val = (value_sum[best_idx] / visits[best_idx]) if visits[best_idx] else float("-inf")
    return {
        "best_variant": variants[best_idx],
        "best_value": best_val,
        "rollouts": total,
        "depth": depth,
        "uct_c": uct_c,
        "visits": sum(visits),
        "explored": sum(1 for v in visits if v > 0),
        "seed": eff_seed,
        "error": None,
        "child_stats": [
            {
                "index": i,
                "visits": visits[i],
                "value_sum": value_sum[i],
                "value_mean": (value_sum[i] / visits[i]) if visits[i] else 0.0,
            }
            for i in range(n)
        ],
    }

