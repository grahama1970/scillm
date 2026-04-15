__all__ = [
    "__version__",
    "MemoryClient",
    # Theory of Mind (ToM) API for persona agents
    "get_or_create_user",
    "update_user_profile",
    "get_user_history",
    "get_or_create_relationship",
    "update_relationship",
    "record_key_moment",
    "get_or_create_persona_state",
    "update_persona_state",
    "get_persona_state_trend",
]
__version__ = "0.1.0"

from loguru import logger

# Primary API - MemoryClient
try:
    from .api import MemoryClient
except Exception as exc:
    logger.error("MemoryClient import unavailable: {}", exc)
    MemoryClient = None  # type: ignore

# Theory of Mind (ToM) API for persona agents
try:
    from .api import (
        get_or_create_user,
        update_user_profile,
        get_user_history,
        get_or_create_relationship,
        update_relationship,
        record_key_moment,
        get_or_create_persona_state,
        update_persona_state,
        get_persona_state_trend,
    )
except Exception as exc:
    logger.error("ToM API import unavailable: {}", exc)
    # ToM functions not available (missing dependencies)
    get_or_create_user = None  # type: ignore
    update_user_profile = None  # type: ignore
    get_user_history = None  # type: ignore
    get_or_create_relationship = None  # type: ignore
    update_relationship = None  # type: ignore
    record_key_moment = None  # type: ignore
    get_or_create_persona_state = None  # type: ignore
    update_persona_state = None  # type: ignore
    get_persona_state_trend = None  # type: ignore

# Optional convenience re-exports for shared Arango helpers
try:  # pragma: no cover
    from .arango_utils import (
        key_control as ar_key_control,
        key_url as ar_key_url,
        key_chunk as ar_key_chunk,
        get_arango_client as ar_get_client,
        ensure_collections_and_view as ar_ensure,
        upsert as ar_upsert,
        upsert_edge as ar_upsert_edge,
        now_utc_iso as ar_now,
    )
    __all__ += [
        "ar_key_control",
        "ar_key_url",
        "ar_key_chunk",
        "ar_get_client",
        "ar_ensure",
        "ar_upsert",
        "ar_upsert_edge",
        "ar_now",
    ]
except Exception as exc:
    logger.error("arango_utils import unavailable: {}", exc)

# Inference Router (tiered model selection)
try:
    from .llm.router import (
        InferenceRouter,
        TierConfig,
        RouteResult,
        Tier,
        edge_score_router,
        qra_assess_router,
        monitor_router,
        edge_verify_router,
        paraphrase_router,
    )
    __all__ += [
        "InferenceRouter",
        "TierConfig",
        "RouteResult",
        "Tier",
        "edge_score_router",
        "qra_assess_router",
        "monitor_router",
        "edge_verify_router",
        "paraphrase_router",
    ]
except Exception as exc:
    logger.error("InferenceRouter import unavailable: {}", exc)

# Teacher-Student Loop (knowledge distillation for monitor-* skills)
try:
    from .llm.teacher_student import (
        TeacherStudentLoop,
        TeacherStudentState,
        AnnealSchedule,
        stratified_sample,
        check_agreement,
        QRA_TEACHER_PROMPT,
        EDGE_SCORE_TEACHER_PROMPT,
        MONITOR_HEALTH_TEACHER_PROMPT,
        EDGE_VERIFY_TEACHER_PROMPT,
    )
    __all__ += [
        "TeacherStudentLoop",
        "TeacherStudentState",
        "AnnealSchedule",
        "stratified_sample",
        "check_agreement",
        "QRA_TEACHER_PROMPT",
        "EDGE_SCORE_TEACHER_PROMPT",
        "MONITOR_HEALTH_TEACHER_PROMPT",
        "EDGE_VERIFY_TEACHER_PROMPT",
    ]
except Exception as exc:
    logger.error("TeacherStudentLoop import unavailable: {}", exc)
