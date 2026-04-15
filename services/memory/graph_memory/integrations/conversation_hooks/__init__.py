"""Conversation hooks for automatic ToM updates.

This package provides hooks that can be called after persona conversations
to automatically update trust, respect, and relationship state based on
interaction quality.

Usage:
    from graph_memory.integrations.conversation_hooks import after_conversation

    # After each conversation turn with Horus persona
    after_conversation(
        user_id="graham",
        agent_id="horus",
        user_message="Tell me about the siege of Terra",
        agent_response="*measured tone* The siege... very well...",
        conversation_quality="competent"  # or "insightful", "dismissive", "hostile"
    )
"""

from graph_memory.integrations.conversation_hooks._hooks import (
    QUALITY_IMPACTS,
    ConversationQuality,
    after_conversation,
    horus_conversation_hook,
    evolve_persona_state,
)
from graph_memory.integrations.conversation_hooks._identity import (
    is_user_known,
    record_user_identity,
    append_user_note,
    should_use_name,
)
from graph_memory.integrations.conversation_hooks._assessment import (
    evaluate_conversation_quality,
    assess_user_utility,
    assess_code_contribution,
)
from graph_memory.integrations.conversation_hooks._learning import (
    LESSON_CATEGORIES,
    learn_about_user,
    recall_user_lessons,
    traverse_user_knowledge,
    weaken_lesson,
)
from graph_memory.integrations.conversation_hooks._context import (
    infer_user_context,
    find_commiseration_memories,
    horus_pre_response_check,
)
from graph_memory.integrations.conversation_hooks._debrief import (
    conversation_debrief,
    horus_conversation_debrief,
    episodic_archiver_post_hook,
    deep_user_association,
    schedule_deep_analysis,
)

__all__ = [
    # Constants
    "QUALITY_IMPACTS",
    "ConversationQuality",
    "LESSON_CATEGORIES",
    # Core hooks
    "after_conversation",
    "horus_conversation_hook",
    "evolve_persona_state",
    # Identity
    "is_user_known",
    "record_user_identity",
    "append_user_note",
    "should_use_name",
    # Assessment
    "evaluate_conversation_quality",
    "assess_user_utility",
    "assess_code_contribution",
    # Learning
    "learn_about_user",
    "recall_user_lessons",
    "traverse_user_knowledge",
    "weaken_lesson",
    # Context
    "infer_user_context",
    "find_commiseration_memories",
    "horus_pre_response_check",
    # Debrief
    "conversation_debrief",
    "horus_conversation_debrief",
    "episodic_archiver_post_hook",
    "deep_user_association",
    "schedule_deep_analysis",
]
