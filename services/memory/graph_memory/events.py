from __future__ import annotations
import time
from typing import Any, Dict
from loguru import logger

def log_event(db, kind: str, message: str, data: Dict[str, Any] | None = None) -> None:
    try:
        doc = {
            'kind': kind,
            'message': message,
            'data': data or {},
            'at': int(time.time()),
        }
        db.collection('memory_events').insert(doc)
    except Exception as exc:
        logger.error("Suppressed error in events: {}", exc)

