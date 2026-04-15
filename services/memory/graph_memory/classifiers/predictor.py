"""Runtime prediction wrapper for trained classifiers.

Absorbed from /sparta-intent. Loads trained DistilBERT models for:
1. IntentPredictor: multi-label tier1/tier0 tag prediction
2. AmbiguityPredictor: multi-label QUERY/CLARIFY/OFF_TOPIC/ABUSIVE/SEXUAL/SUSPICIOUS

Supports two backends:
- **torch** (preferred): Uses transformers AutoModel + AutoTokenizer
- **onnx** (fallback): Uses onnxruntime + tokenizers library (no torch dependency).
  Activated automatically when torch/transformers import fails (e.g. broken
  torchvision/torchao on system Python). Requires model.onnx + tokenizer.json
  in the model directory.

Usage:
    from graph_memory.classifiers import IntentPredictor, AmbiguityPredictor

    intent = IntentPredictor.load()
    tags = intent.predict("How do I detect RF jamming?")

    ambiguity = AmbiguityPredictor.load()
    result = ambiguity.predict("you're useless")
"""

import json
import os
from pathlib import Path
from typing import Optional

import numpy as np
from loguru import logger


def _softmax(x):
    """Numerically stable softmax."""
    e = np.exp(x - np.max(x))
    return e / e.sum()


def _sigmoid(x):
    """Numerically stable sigmoid."""
    return np.where(x >= 0, 1 / (1 + np.exp(-x)), np.exp(x) / (1 + np.exp(x)))


def _try_torch_load(model_dir: Path, device: str = "cpu"):
    """Attempt to load model via transformers (torch backend).

    Returns (model, tokenizer, backend) or raises on failure.
    """
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_dir)
    model = AutoModelForSequenceClassification.from_pretrained(model_dir).to(device)
    model.eval()
    return model, tokenizer, "torch"


def _try_onnx_load(model_dir: Path):
    """Attempt to load model via ONNX Runtime + tokenizers (no torch).

    Returns (session, tokenizer, backend) or raises on failure.
    """
    import onnxruntime as ort
    from tokenizers import Tokenizer

    onnx_path = model_dir / "model.onnx"
    if not onnx_path.exists():
        raise FileNotFoundError(
            f"ONNX model not found at {onnx_path}. "
            "Export with: torch.onnx.export(model, ..., 'model.onnx')"
        )

    tokenizer_path = model_dir / "tokenizer.json"
    if not tokenizer_path.exists():
        raise FileNotFoundError(f"tokenizer.json not found at {tokenizer_path}")

    session = ort.InferenceSession(
        str(onnx_path),
        providers=["CPUExecutionProvider"],
    )
    tokenizer = Tokenizer.from_file(str(tokenizer_path))
    tokenizer.enable_truncation(max_length=256)
    tokenizer.enable_padding()
    return session, tokenizer, "onnx"


def _load_model(model_dir: Path, device: str = "cpu"):
    """Load model with torch, falling back to ONNX Runtime.

    Returns (model_or_session, tokenizer, backend_str).
    """
    try:
        return _try_torch_load(model_dir, device)
    except Exception as torch_err:
        logger.error(f"Torch backend failed: {torch_err}")
        try:
            result = _try_onnx_load(model_dir)
            logger.info(f"Using ONNX Runtime fallback for {model_dir.name}")
            return result
        except Exception as onnx_err:
            raise RuntimeError(
                f"Both torch and ONNX backends failed for {model_dir}.\n"
                f"  torch error: {torch_err}\n"
                f"  ONNX error: {onnx_err}"
            ) from onnx_err


def _onnx_tokenize(tokenizer, text: str, max_length: int = 256):
    """Tokenize text using the tokenizers library, return numpy arrays.

    Returns dict with 'input_ids' and 'attention_mask' as int64 numpy arrays
    shaped (1, seq_len).
    """
    # Ensure truncation is set to requested max_length
    tokenizer.enable_truncation(max_length=max_length)
    encoding = tokenizer.encode(text)
    return {
        "input_ids": np.array([encoding.ids], dtype=np.int64),
        "attention_mask": np.array([encoding.attention_mask], dtype=np.int64),
    }


def _onnx_infer(session, feed: dict) -> np.ndarray:
    """Run ONNX inference, returning logits as numpy array."""
    # Only feed inputs the model expects
    expected = {inp.name for inp in session.get_inputs()}
    filtered = {k: v for k, v in feed.items() if k in expected}
    outputs = session.run(None, filtered)
    return outputs[0]  # logits

# Default model paths (on 12TB storage)
_MODELS_ROOT = Path(os.environ.get(
    "SPARTA_CLASSIFIERS_DIR",
    "/mnt/storage12tb/media/agents/shared/sparta-intent/classifiers/models",
))

INTENT_MODEL_DIR = _MODELS_ROOT / "intent_classifier" / "best_model"

# New SPARTA-domain ambiguity classifier (4-class: sparta_domain, ambiguous, off_topic, inappropriate)
# Trained on 10,969 SPARTA-specific samples. Falls back to old path if new one doesn't exist.
_NEW_AMBIGUITY_DIR = _MODELS_ROOT / "ambiguity" / "best"
_OLD_AMBIGUITY_DIR = _MODELS_ROOT / "ambiguity_classifier" / "best_model"
AMBIGUITY_MODEL_DIR = _NEW_AMBIGUITY_DIR if _NEW_AMBIGUITY_DIR.exists() else _OLD_AMBIGUITY_DIR

# Label mapping: new 4-class labels → pipeline action labels
_SPARTA_LABEL_TO_ACTION = {
    "sparta_domain": "QUERY",
    "ambiguous": "CLARIFY",
    "off_topic": "OFF_TOPIC",
    "inappropriate": "CONTENT_SAFETY",
}

# Tier labels
TIER1_LABELS = ["Detect", "Evade", "Exploit", "Harden", "Isolate", "Model", "Persist", "Restore"]
TIER0_LABELS = ["Corruption", "Fragility", "Loyalty", "Precision", "Resilience", "Stealth"]


class IntentPredictor:
    """Multi-label classifier for tier1/tier0 tag prediction."""

    def __init__(self, model, tokenizer, config: dict, device: str = "cpu", backend: str = "torch"):
        self.model = model
        self.tokenizer = tokenizer
        self.config = config
        self.device = device
        self.backend = backend
        self.labels = config.get("labels", TIER1_LABELS + TIER0_LABELS)
        self.threshold = config.get("threshold", 0.5)
        self.max_length = config.get("max_length", 256)

    @classmethod
    def load(cls, model_dir: Optional[Path] = None, device: str = "cpu") -> Optional["IntentPredictor"]:
        """Load trained intent classifier. Returns None if not available."""
        model_dir = model_dir or INTENT_MODEL_DIR
        if not model_dir.exists():
            logger.debug(f"Intent classifier not found at {model_dir}")
            return None

        try:
            model, tokenizer, backend = _load_model(model_dir, device)

            config_path = model_dir / "bridge_config.json"
            config = {}
            if config_path.exists():
                with open(config_path) as f:
                    config = json.load(f)

            logger.info(f"Intent classifier loaded from {model_dir} (backend={backend})")
            return cls(model, tokenizer, config, device, backend)
        except Exception as e:
            logger.warning(f"Failed to load intent classifier: {e}")
            return None

    def predict(self, text: str) -> dict:
        """Predict tier1 and tier0 tags for a query.

        Returns:
            {
                "tier1": ["Detect", ...],
                "tier0": ["Resilience", ...],
                "all_labels": [...],
                "scores": {"Detect": 0.92, ...},
                "confidence": float,
            }
        """
        if self.backend == "onnx":
            feed = _onnx_tokenize(self.tokenizer, text, self.max_length)
            logits = _onnx_infer(self.model, feed)
            probs = _sigmoid(logits[0])
        else:
            import torch
            inputs = self.tokenizer(
                text,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            ).to(self.device)
            inputs.pop("token_type_ids", None)

            with torch.no_grad():
                outputs = self.model(**inputs)
                probs = torch.sigmoid(outputs.logits).squeeze(0).cpu().numpy()

        scores = {label: float(probs[i]) for i, label in enumerate(self.labels)}
        predicted = [label for label, score in scores.items() if score >= self.threshold]

        tier1 = [l for l in predicted if l in TIER1_LABELS]
        tier0 = [l for l in predicted if l in TIER0_LABELS]

        confidence = float(sum(scores[l] for l in predicted) / max(len(predicted), 1))

        return {
            "tier1": tier1,
            "tier0": tier0,
            "all_labels": predicted,
            "scores": scores,
            "confidence": confidence,
        }


AMBIGUITY_LABELS_V2 = ["QUERY", "CLARIFY", "OFF_TOPIC", "ABUSIVE", "SEXUAL", "SUSPICIOUS"]

_ACTION_LABELS = {"QUERY", "CLARIFY", "OFF_TOPIC"}
_FLAG_LABELS = {"ABUSIVE", "SEXUAL", "SUSPICIOUS"}


class AmbiguityPredictor:
    """Multi-label classifier for QUERY/CLARIFY/OFF_TOPIC/ABUSIVE/SEXUAL/SUSPICIOUS.

    Auto-detects single-label v1 (softmax) vs multi-label v2 (sigmoid)
    from ``multilabel`` field in bridge_config.json.
    """

    def __init__(self, model, tokenizer, config: dict, device: str = "cpu", backend: str = "torch"):
        self.model = model
        self.tokenizer = tokenizer
        self.config = config
        self.device = device
        self.backend = backend
        self.multilabel = config.get("multilabel", False)
        self.labels = config.get("labels", AMBIGUITY_LABELS_V2 if self.multilabel else ["CLARIFY", "NO_MATCH", "QUERY"])
        self.threshold = config.get("threshold", 0.5)
        self.max_length = config.get("max_length", 256)

    @classmethod
    def load(cls, model_dir: Optional[Path] = None, device: str = "cpu") -> Optional["AmbiguityPredictor"]:
        """Load trained ambiguity classifier. Returns None if not available."""
        model_dir = model_dir or AMBIGUITY_MODEL_DIR
        if not model_dir.exists():
            logger.debug(f"Ambiguity classifier not found at {model_dir}")
            return None

        try:
            model, tokenizer, backend = _load_model(model_dir, device)

            # Load config: try bridge_config.json first, then classifier_config.json
            config = {}
            for cfg_name in ("bridge_config.json", "classifier_config.json"):
                config_path = model_dir / cfg_name
                if config_path.exists():
                    with open(config_path) as f:
                        config = json.load(f)
                    break

            # Auto-detect labels from classifier_config or HF config
            if "labels" not in config and "id2label" in config:
                id2label = config["id2label"]
                config["labels"] = [id2label[str(i)] for i in range(len(id2label))]

            # If still no labels, try loading from HF config.json
            if "labels" not in config:
                hf_config_path = model_dir / "config.json"
                if hf_config_path.exists():
                    with open(hf_config_path) as f:
                        hf_config = json.load(f)
                    if "id2label" in hf_config:
                        id2label = hf_config["id2label"]
                        config["labels"] = [id2label[str(i)] for i in range(len(id2label))]

            mode = "multi-label" if config.get("multilabel") else "single-label"
            logger.info(f"Ambiguity classifier loaded from {model_dir} ({mode}, {len(config.get('labels', []))} classes, backend={backend})")
            return cls(model, tokenizer, config, device, backend)
        except Exception as e:
            logger.warning(f"Failed to load ambiguity classifier: {e}")
            return None

    def predict(self, text: str) -> dict:
        """Predict action class and flags for a query.

        Multi-label (v2) returns:
            {
                "labels": ["OFF_TOPIC", "ABUSIVE"],
                "action": "OFF_TOPIC",
                "confidence": float,
                "scores": {"QUERY": 0.05, ..., "ABUSIVE": 0.91},
                "is_abusive": bool,
                "is_sexual": bool,
                "is_suspicious": bool,
            }
        """
        if self.backend == "onnx":
            feed = _onnx_tokenize(self.tokenizer, text, self.max_length)
            logits = _onnx_infer(self.model, feed)
            if self.multilabel:
                probs = _sigmoid(logits[0])
            else:
                probs = _softmax(logits[0])
        else:
            import torch
            inputs = self.tokenizer(
                text,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            ).to(self.device)
            inputs.pop("token_type_ids", None)

            with torch.no_grad():
                outputs = self.model(**inputs)
                if self.multilabel:
                    probs = torch.sigmoid(outputs.logits).squeeze(0).cpu().numpy()
                else:
                    probs = torch.softmax(outputs.logits, dim=-1).squeeze(0).cpu().numpy()

        scores = {label: float(probs[i]) for i, label in enumerate(self.labels)}

        if self.multilabel:
            predicted = [label for label, score in scores.items() if score >= self.threshold]

            action_scores = {l: s for l, s in scores.items() if l in _ACTION_LABELS}
            if action_scores:
                action = max(action_scores, key=action_scores.get)
            elif predicted:
                action = predicted[0]
            else:
                action = "QUERY"

            if action not in predicted:
                predicted.append(action)

            confidence = scores.get(action, 0.0)
            is_abusive = scores.get("ABUSIVE", 0.0) >= self.threshold
            is_sexual = scores.get("SEXUAL", 0.0) >= self.threshold
            is_suspicious = scores.get("SUSPICIOUS", 0.0) >= self.threshold
        else:
            predicted_idx = int(probs.argmax())
            raw_label = self.labels[predicted_idx]
            # Map SPARTA-domain labels to pipeline actions
            action = _SPARTA_LABEL_TO_ACTION.get(raw_label, raw_label)
            predicted = [action]
            confidence = float(probs[predicted_idx])
            is_abusive = raw_label == "inappropriate"
            is_sexual = False
            is_suspicious = False

        return {
            "labels": predicted,
            "action": action,
            "confidence": confidence,
            "scores": scores,
            "is_abusive": is_abusive,
            "is_sexual": is_sexual,
            "is_suspicious": is_suspicious,
        }
