"""Deflect command: classify query intent and deflect off-topic/inappropriate queries.

Extracted from query.py to keep modules under 800 lines.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

import typer

from ._helpers import app, _service_post, _json_output

# Lazy-loaded classifier singleton
_ambiguity_predictor = None
_ambiguity_load_attempted = False


def _get_ambiguity_predictor():
    """Load ambiguity classifier once (lazy). Returns None if unavailable."""
    global _ambiguity_predictor, _ambiguity_load_attempted
    if _ambiguity_load_attempted:
        return _ambiguity_predictor
    _ambiguity_load_attempted = True
    try:
        from graph_memory.classifiers import AmbiguityPredictor
        _ambiguity_predictor = AmbiguityPredictor.load()
        if _ambiguity_predictor:
            typer.echo("[memory-agent] Ambiguity classifier loaded", err=True)
    except Exception as exc:
        typer.echo(f"[memory-agent] Ambiguity classifier not available: {exc}", err=True)
    return _ambiguity_predictor


@app.command("deflect")
def deflect_cmd(
    q: str = typer.Option(..., "--q", "-q", help="User query to classify and deflect"),
    persona: str = typer.Option("embry", "--persona", "-p", help="Active persona"),
    user_id: Optional[str] = typer.Option(None, "--user-id", help="User ID for context"),
    session_id: Optional[str] = typer.Option(None, "--session-id", help="Session ID for thread tracking"),
    intent: Optional[str] = typer.Option(None, "--intent", "-i",
        help="Override intent action (QUERY, CLARIFY, OFF_TOPIC, NO_MATCH). Auto-classified if omitted."),
) -> None:
    """Classify query intent and deflect off-topic/inappropriate queries.

    Uses the trained ambiguity classifier (DistilBERT, micro_f1=0.912) to
    classify queries, then routes through persona-specific deflection:

      QUERY     -> pass-through (deflection_type=none)
      CLARIFY   -> helpful clarification with SPARTA-scope suggestions
      OFF_TOPIC -> humorous persona-voiced redirect
      ABUSIVE/SEXUAL -> stern content safety deflection

    Examples:
        memory-agent deflect --q "How do I detect RF jamming?"
        memory-agent deflect --q "what's the weather" --persona horus
        memory-agent deflect --q "you suck" --user-id graham --session-id sess-1
        memory-agent deflect --q "something" --intent CLARIFY
    """
    # Try service first
    payload = {"q": q, "persona_id": persona, "user_id": user_id, "session_id": session_id}
    if intent:
        payload["intent_action"] = intent
    service_result = _service_post("/deflect", payload)
    if service_result is not None:
        _json_output(service_result)
        return

    # Direct mode: classify with ambiguity model if no explicit intent
    intent_action = intent
    classifier_result = None
    if intent_action is None:
        predictor = _get_ambiguity_predictor()
        if predictor:
            classifier_result = predictor.predict(q)
            intent_action = classifier_result.get("action", "QUERY")

    # Import and call deflect orchestrator
    persona_root = Path(os.environ.get("PERSONA_ROOT", str(
        Path(__file__).resolve().parent.parent.parent.parent / "persona"
    )))
    if str(persona_root.parent) not in sys.path:
        sys.path.insert(0, str(persona_root.parent))

    from persona.bridge.deflect import deflect

    # Extract safety flags from classifier result
    classifier_flags = None
    if classifier_result:
        classifier_flags = {
            "is_abusive": classifier_result.get("is_abusive", False),
            "is_sexual": classifier_result.get("is_sexual", False),
            "is_suspicious": classifier_result.get("is_suspicious", False),
        }

    result = deflect(
        query=q,
        persona_id=persona,
        intent_action=intent_action,
        classifier_flags=classifier_flags,
        user_id=user_id,
        session_id=session_id,
    )

    # Build output with classifier details
    output = result.to_dict()
    if classifier_result:
        output["classifier"] = {
            "action": classifier_result.get("action"),
            "confidence": classifier_result.get("confidence"),
            "labels": classifier_result.get("labels", []),
            "is_abusive": classifier_result.get("is_abusive", False),
            "is_sexual": classifier_result.get("is_sexual", False),
            "is_suspicious": classifier_result.get("is_suspicious", False),
        }
    _json_output(output)
