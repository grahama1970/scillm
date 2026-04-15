"""2-way intent classifier: RECALL vs OFF_TOPIC.

The classifier's ONLY job is to distinguish:
- RECALL → problem-solving queries that should trigger memory search
- OFF_TOPIC → chitchat that should be politely deflected

COMPLIANCE and APP_COMMAND routing is handled BEFORE this classifier runs,
via deterministic gates in _mapper.py:
- /extract-entities finds framework references (CWE, NIST, CAPEC, SPARTA) → COMPLIANCE
- Slash commands (/skill) or UI verbs (zoom, select) → APP_COMMAND

The 2-way task achieves 96.5% F1 vs ~70% for 4-way. Simpler is better.

Classification priority:
1. T0 deterministic rules (problem signals, greeting signals)
2. T1.5 bert-tiny classifier via inference service (96.5% F1, ~1ms)
3. Fallback to RECALL if uncertain (fail open to memory search)
"""
from __future__ import annotations

import os
from typing import Optional

import httpx
from loguru import logger


INFERENCE_URL = os.getenv("INFERENCE_SERVICE_URL", "http://localhost:8605")
INFERENCE_TIMEOUT = float(os.getenv("INFERENCE_TIMEOUT", "5.0"))

# 2-way labels
LABELS = ["OFF_TOPIC", "RECALL"]

# Mapping from classifier labels to pipeline actions
LABEL_TO_ACTION = {
    "RECALL": "QUERY",
    "OFF_TOPIC": "NO_MATCH",
}


def classify_2way_deterministic(query: str) -> dict | None:
    """T0 deterministic gate for clear-cut cases.
    
    Returns classification if high confidence, None if uncertain (needs ML).
    
    RECALL signals (problem-solving):
    - Question words: how, why, what, where, when, which
    - Problem keywords: fix, error, debug, fail, broken, issue
    - Help requests: help, can't, unable, trouble
    
    OFF_TOPIC signals (chitchat):
    - Greetings: hello, hi, hey, bye, thanks
    - Single words < 15 chars without question marks
    - Social phrases: how are you, what's up
    """
    q_lower = query.lower().strip()
    q_stripped = q_lower.rstrip('?!.,')
    
    # ============ OFF_TOPIC: Clear chitchat ============
    # Short greetings/acknowledgments
    off_topic_exact = {
        'hello', 'hi', 'hey', 'yo', 'sup',
        'bye', 'goodbye', 'later', 'cya',
        'thanks', 'thank you', 'thx', 'ty',
        'ok', 'okay', 'k', 'yes', 'no', 'yep', 'nope', 'yeah', 'nah',
        'sure', 'cool', 'great', 'nice', 'awesome', 'perfect',
        'got it', 'understood', 'agreed', 'right', 'correct',
        'hmm', 'huh', 'oh', 'ah', 'wow',
    }
    if q_stripped in off_topic_exact:
        logger.debug("T0 deterministic: OFF_TOPIC (exact match: '{}')", q_stripped)
        return {
            "action": "NO_MATCH",
            "label": "OFF_TOPIC",
            "confidence": 0.98,
            "classifier_source": "2way_deterministic",
        }
    
    # Social chitchat patterns
    chitchat_patterns = (
        "how are you", "how's it going", "what's up", "how do you do",
        "nice to meet", "good morning", "good afternoon", "good evening",
        "have a good", "take care", "see you",
    )
    if any(q_lower.startswith(p) for p in chitchat_patterns):
        logger.debug("T0 deterministic: OFF_TOPIC (chitchat pattern: '{}')", q_lower[:40])
        return {
            "action": "NO_MATCH",
            "label": "OFF_TOPIC",
            "confidence": 0.95,
            "classifier_source": "2way_deterministic",
        }
    
    # ============ RECALL: Clear problem-solving ============
    # Question patterns
    question_starts = (
        "how do i", "how to", "how can i", "how should i",
        "why does", "why is", "why do", "why did", "why are", "why won't",
        "what is the", "what does", "what causes", "what's wrong",
        "where is", "where do", "where can",
        "when should", "when do", "when does",
        "which ", "who ",
        "can i", "can you", "could you", "would you",
        "is there", "are there", "is it possible",
    )
    if any(q_lower.startswith(p) for p in question_starts):
        logger.debug("T0 deterministic: RECALL (question pattern: '{}')", q_lower[:40])
        return {
            "action": "QUERY",
            "label": "RECALL",
            "confidence": 0.90,
            "classifier_source": "2way_deterministic",
        }
    
    # Problem keywords (strong signal for RECALL)
    problem_signals = (
        "fix ", "debug", "error", "fail", "broken", "crash",
        "issue", "problem", "bug ", "doesn't work", "not working",
        "can't ", "cannot ", "won't ", "unable ",
        "help me", "help with", "stuck ", "trouble",
        "status of", "check the", "what went wrong",
    )
    if any(sig in q_lower for sig in problem_signals):
        logger.debug("T0 deterministic: RECALL (problem signal in: '{}')", q_lower[:40])
        return {
            "action": "QUERY",
            "label": "RECALL",
            "confidence": 0.92,
            "classifier_source": "2way_deterministic",
        }
    
    # Questions ending with ? are likely RECALL
    if query.rstrip().endswith('?') and len(query) > 15:
        logger.debug("T0 deterministic: RECALL (question mark, len={})", len(query))
        return {
            "action": "QUERY", 
            "label": "RECALL",
            "confidence": 0.85,
            "classifier_source": "2way_deterministic",
        }
    
    # Uncertain — need ML classifier
    return None


def classify_2way(query: str) -> Optional[dict]:
    """Classify query as RECALL or OFF_TOPIC via inference service.
    
    Calls embry-inference service at /v1/classify with memory-intent model.
    Falls back to RECALL (fail open) if service unavailable.
    
    Returns:
        dict with keys: action, label, confidence, classifier_source
        None if classification fails (caller should default to RECALL)
    """
    try:
        with httpx.Client(timeout=INFERENCE_TIMEOUT) as client:
            response = client.post(
                f"{INFERENCE_URL}/v1/classify",
                json={
                    "model": "memory-intent",
                    "inputs": [{"text": query}],
                },
            )
            response.raise_for_status()
            data = response.json()
        
        if not data.get("results"):
            logger.warning("Empty results from inference service")
            return None
        
        result = data["results"][0]
        label = result["label"]
        confidence = result["confidence"]
        
        action = LABEL_TO_ACTION.get(label, "QUERY")  # Default to QUERY (RECALL)
        
        logger.info(
            "2-way ML classifier: {} (conf={:.2f}, latency={}ms, query='{}')",
            label, confidence, data.get("latency_ms", "?"), query[:60],
        )
        
        return {
            "action": action,
            "label": label,
            "confidence": confidence,
            "classifier_source": "2way_inference_service",
        }
    
    except httpx.RequestError as e:
        logger.warning("Inference service unavailable: {}", e)
        return None
    except httpx.HTTPStatusError as e:
        logger.warning("Inference service error: {} {}", e.response.status_code, e.response.text[:100])
        return None
    except Exception as e:
        logger.error("Unexpected error calling inference service: {}", e)
        return None


def classify_intent(query: str) -> dict:
    """Main entry point: classify query intent.
    
    Priority:
    1. T0 deterministic rules (~0ms)
    2. T1.5 ML classifier (~1ms)
    3. Default to RECALL (fail open to memory search)
    
    Note: COMPLIANCE and APP_COMMAND are routed by _mapper.py BEFORE this runs.
    This classifier only handles the RECALL vs OFF_TOPIC decision.
    """
    # T0: Deterministic rules
    det_result = classify_2way_deterministic(query)
    if det_result is not None:
        return det_result
    
    # T1.5: ML classifier
    ml_result = classify_2way(query)
    if ml_result is not None:
        return ml_result
    
    # Fallback: Default to RECALL (fail open - memory search is safe)
    logger.info("Classifier fallback: defaulting to RECALL for query='{}'", query[:60])
    return {
        "action": "QUERY",
        "label": "RECALL",
        "confidence": 0.5,
        "classifier_source": "2way_fallback",
    }


# Legacy aliases for compatibility
def classify_4way(query: str) -> Optional[dict]:
    """DEPRECATED: Use classify_intent() instead."""
    return classify_2way(query)


def classify_4way_deterministic(
    query: str,
    entity_count: int = 0,
    framework_count: int = 0,
    recall_confidence: float = 0.0,
    recall_top_source: str = "",
) -> dict | None:
    """DEPRECATED: Use classify_2way_deterministic() instead.
    
    COMPLIANCE and APP_COMMAND routing is now handled by deterministic gates
    in _mapper.py using /extract-entities, not this classifier.
    """
    return classify_2way_deterministic(query)


def is_available() -> bool:
    """Check if the inference service is available."""
    try:
        import httpx
        with httpx.Client(timeout=2.0) as client:
            response = client.get(f"{INFERENCE_URL}/health")
            return response.status_code == 200
    except Exception:
        return False
