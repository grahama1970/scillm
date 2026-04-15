"""T0.5 SFT intent classifier (Qwen2.5-1.5B + QLoRA).

Scope-aware: loads different adapters per calling surface.
- sparta (default): 2-class QUERY/NO_MATCH, Mind tags. 93.7% accuracy.
- binary-explorer: 4-class QUERY/UI_COMMAND/NO_MATCH/CLARIFY.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from loguru import logger

from ._spec import QuerySpec

_MODELS_ROOT = Path(
    "/mnt/storage12tb/media/agents/shared/sparta-intent/classifiers/models"
)
ADAPTER_PATH = _MODELS_ROOT / "intent_classifier"
BINEX_ADAPTER_PATH = _MODELS_ROOT / "binex_intent_classifier"
BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"

_SPARTA_SYSTEM_PROMPT = (
    "You are a SPARTA space cybersecurity intent classifier. "
    "Given a question, classify it as on-topic (QUERY) or off-topic (NO_MATCH) "
    "and extract Mind tags.\n\n"
    "ACTIONS:\n"
    "- QUERY: space cybersecurity question — including vague, meta, or cross-framework questions\n"
    "- NO_MATCH: off-topic, fabricated control IDs, or unrelated to space cybersecurity\n\n"
    "MIND TAGS (ONLY from this list): Detect, Evade, Exploit, Harden, Isolate, Model, Persist, Restore\n\n"
    'Return ONLY valid JSON: {"action": "...", "tier1": [...], "entities": [...], "lanes": [...], "confidence": 0.0-1.0}'
)

_BINEX_SYSTEM_PROMPT = (
    "You are a Binary Explorer intent classifier for reverse engineering. "
    "Given a natural language request, classify it and extract structured parameters.\n\n"
    "ACTIONS:\n"
    "- QUERY: analysis question about binary features, relationships, security\n"
    "- UI_COMMAND: graph manipulation — zoom, select, expand, perspective, scene action\n"
    "- NO_MATCH: off-topic, not about binary analysis\n"
    "- CLARIFY: too vague, needs more detail\n\n"
    'Return ONLY valid JSON: {"action": "...", "ui_action": "...", "entities": [...], "keywords": [...], "confidence": 0.0-1.0}'
)

_SCOPE_CONFIG: dict[str, dict] = {
    "sparta": {"path": ADAPTER_PATH, "prompt": _SPARTA_SYSTEM_PROMPT, "default_scope": "sparta"},
    "binary-explorer": {"path": BINEX_ADAPTER_PATH, "prompt": _BINEX_SYSTEM_PROMPT, "default_scope": "binary-explorer"},
}


class T05Classifier:
    """Thin wrapper around the QLoRA-adapted Qwen2.5-1.5B intent classifier."""

    def __init__(self, model, tokenizer, scope: str = "sparta"):
        self._model = model
        self._tokenizer = tokenizer
        self._scope = scope
        cfg = _SCOPE_CONFIG.get(scope, _SCOPE_CONFIG["sparta"])
        self._system_prompt = cfg["prompt"]
        self._default_scope = cfg["default_scope"]

    @classmethod
    def load(cls, scope: str = "sparta") -> Optional["T05Classifier"]:
        """Load base model + QLoRA adapter for the given scope."""
        cfg = _SCOPE_CONFIG.get(scope, _SCOPE_CONFIG["sparta"])
        adapter_path = cfg["path"]
        if not (adapter_path / "adapter_model.safetensors").exists():
            logger.debug(f"T0.5 adapter not found at {adapter_path} (scope={scope})")
            return None

        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer

        logger.info(f"Loading T0.5 classifier: {BASE_MODEL} + {adapter_path.name} (scope={scope})")
        tokenizer = AutoTokenizer.from_pretrained(BASE_MODEL, trust_remote_code=True)
        model = AutoModelForCausalLM.from_pretrained(
            BASE_MODEL, device_map="auto", torch_dtype="auto", trust_remote_code=True,
        )
        model = PeftModel.from_pretrained(model, str(adapter_path))
        model.eval()
        logger.info(f"T0.5 intent classifier loaded (scope={scope})")
        return cls(model, tokenizer, scope=scope)

    def predict(self, query: str) -> dict | None:
        """Run inference. Returns QuerySpec dict or None on parse failure."""
        import torch

        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": query},
        ]
        text = self._tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
        )
        inputs = self._tokenizer(text, return_tensors="pt").to(self._model.device)

        with torch.no_grad():
            outputs = self._model.generate(
                **inputs, max_new_tokens=256, do_sample=False,
                temperature=1.0, pad_token_id=self._tokenizer.eos_token_id,
            )

        generated = outputs[0][inputs["input_ids"].shape[1]:]
        response = self._tokenizer.decode(generated, skip_special_tokens=True).strip()

        try:
            if "```" in response:
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            parsed = json.loads(response)
            spec = QuerySpec(**{
                "action": parsed.get("action", "QUERY"),
                "ui_action": parsed.get("ui_action"),
                "tier1": parsed.get("tier1", []),
                "entities": parsed.get("entities", []),
                "lanes": parsed.get("lanes", ["bm25", "dense"]),
                "keywords": parsed.get("keywords", []),
                "scope": self._default_scope,
            })
            logger.debug(f"T0.5[{self._scope}] classified '{query[:50]}' as {spec.action}")
            return spec.model_dump()
        except (json.JSONDecodeError, Exception) as exc:
            logger.error(f"T0.5 parse failed: {exc}")
            return None
