from __future__ import annotations
import time
import os
from typing import Any
import re
from loguru import logger


def _lesson_key(lesson_id: str) -> str:
    try:
        return lesson_id.split('/')[-1]
    except Exception as exc:
        logger.error("Failed to extract lesson key from {}: {}", lesson_id, exc)
        return lesson_id


def _scrub_text_if_enabled(text: str) -> tuple[str, bool]:
    """Redact common PII patterns when PII_SCRUB is enabled.

    Behavior
    - If PII_SCRUB=1, applies conservative regex redactions with placeholders.
    - Optional allowlist via PII_SCRUB_ALLOW (comma-separated regex): protect matching spans from redaction.
    - Optional denylist via PII_SCRUB_DENY (comma-separated regex): additionally redact matches as [REDACTED:DENY].

    Returns (scrubbed_text, did_change)
    """
    import os as _os

    if _os.getenv('PII_SCRUB') not in ('1', 'true', 'TRUE'):
        return text, False

    def _compile_list(env_name: str) -> list[re.Pattern[str]]:
        raw = (_os.getenv(env_name) or '').strip()
        if not raw:
            return []
        pats: list[re.Pattern[str]] = []
        for part in (p.strip() for p in raw.split(',') if p.strip()):
            try:
                pats.append(re.compile(part))
            except Exception as exc:
                # Skip invalid patterns to keep happy path stable
                logger.error("Invalid PII scrub pattern skipped: {}", exc)
                continue
        return pats

    allow_pats = _compile_list('PII_SCRUB_ALLOW')
    deny_pats = _compile_list('PII_SCRUB_DENY')

    s = text

    # 1) Protect allowlisted spans with sentinels so they are not scrubbed.
    protected: list[str] = []
    def _protect_fn(m: re.Match[str]) -> str:
        idx = len(protected)
        token = f"<<ALW_{idx}>>"
        protected.append(m.group(0))
        return token

    for ap in allow_pats:
        s = ap.sub(_protect_fn, s)

    # 2) Built-in redactions
    # Email addresses
    s = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED:EMAIL]", s)
    # API keys (simple patterns: sk-..., long hex/base62 tokens)
    s = re.sub(r"\bsk-[A-Za-z0-9]{8,}\b", "[REDACTED:KEY]", s)
    s = re.sub(r"\b[A-Fa-f0-9]{32,}\b", "[REDACTED:KEY]", s)
    # Phone numbers (very loose: 9-15 digits with separators)
    s = re.sub(r"\b\+?\d[\d\-\s]{8,}\d\b", "[REDACTED:PHONE]", s)
    # IPv4 addresses
    s = re.sub(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "[REDACTED:IP]", s)

    # 3) Denylist extras
    for dp in deny_pats:
        try:
            s = dp.sub("[REDACTED:DENY]", s)
        except Exception as exc:
            # Keep going on malformed patterns
            logger.error("PII deny pattern substitution failed: {}", exc)
            continue

    # 4) Restore protected allowlisted spans
    for i, original in enumerate(protected):
        s = s.replace(f"<<ALW_{i}>>", original)

    return s, (s != text)


def store_text(db: Any, lesson_id: str, scope: str, text: str, *, page_count: int = 0, source: str = 'arxiv') -> dict[str, Any]:
    """Store extracted text in a separate collection keyed by lesson.

    Environment controls:
    - ARXIV_TEXT_TTL_DAYS: if >0, sets expires_at to now + days*86400 (TTL index purges later)
    - ARXIV_TEXT_MAX_CHARS: caller clamps text length; this function writes as-is
    """
    if not text:
        return { 'ok': False, 'reason': 'empty' }
    ts = int(time.time())
    key = _lesson_key(lesson_id)
    ttl_days = int(os.getenv('ARXIV_TEXT_TTL_DAYS', '0') or '0')
    expires_at = ts + ttl_days * 86400 if ttl_days > 0 else None
    # Optional sentence-level chunking for downstream retrieval; off by default
    chunks_meta: list[dict[str, Any]] | None = None
    if os.getenv('ARXIV_STORE_TEXT_CHUNKS') in ('1','true','TRUE'):
        try:
            from .chunking import chunk_text_sentences
            max_chars = int(os.getenv('TEXT_CHUNK_MAX_CHARS', '1200'))
            overlap = int(os.getenv('TEXT_CHUNK_OVERLAP', '120'))
            # Apply scrub before chunking to avoid leaking raw tokens
            scrubbed_text, scrubbed = _scrub_text_if_enabled(text)
            chunks_meta = chunk_text_sentences(scrubbed_text, max_chars=max_chars, overlap=overlap)
            text = scrubbed_text
        except Exception as exc:
            logger.error("Text chunking failed: {}", exc)
            chunks_meta = None
    else:
        # Even without chunking, scrub if enabled
        scrubbed_text, scrubbed = _scrub_text_if_enabled(text)
        text = scrubbed_text

    doc = {
        '_key': key,
        'lesson_id': lesson_id,
        'scope': scope,
        'source': source,
        'page_count': int(page_count),
        'text': text,
        'created_at': ts,
        'updated_at': ts,
    }
    if os.getenv('PII_SCRUB') in ('1','true','TRUE'):
        doc['scrubbed'] = True
    if expires_at:
        doc['expires_at'] = int(expires_at)
    if chunks_meta:
        doc['text_chunks'] = chunks_meta
    try:
        db.collection('lesson_texts').insert(doc)
        return { 'ok': True, 'action': 'insert' }
    except Exception as exc:
        logger.error("lesson_texts insert failed, trying update: {}", exc)
        try:
            db.collection('lesson_texts').update(doc)
            return { 'ok': True, 'action': 'update' }
        except Exception as e:
            return { 'ok': False, 'error': str(e) }
