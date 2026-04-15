from __future__ import annotations
import re
from typing import List, Dict, Any
from loguru import logger

_ABBR = set([
    'e.g.', 'i.e.', 'etc.', 'mr.', 'mrs.', 'dr.', 'prof.', 'vs.', 'al.', 'fig.', 'eq.', 'sec.', 'vol.', 'no.', 'pp.'
])


def _split_sentences(text: str) -> List[str]:
    # Fast path: spaCy if available
    try:
        import spacy  # type: ignore
        try:
            nlp = spacy.load('en_core_web_sm')
        except Exception as exc:
            logger.error("_split_sentences load failed: {exc}", exc=exc)
            # fall back to blank with sentencizer
            nlp = spacy.blank('en')
            nlp.add_pipe('sentencizer')
        doc = nlp(text)
        return [s.text.strip() for s in doc.sents if s.text and s.text.strip()]
    except Exception as exc:
        logger.error("_split_sentences caught error: {exc}", exc=exc)
    # Next: NLTK punkt if available
    try:
        import nltk  # type: ignore
        try:
            nltk.data.find('tokenizers/punkt')
        except LookupError:
            nltk.download('punkt', quiet=True)
        from nltk.tokenize import sent_tokenize  # type: ignore
        return [s.strip() for s in sent_tokenize(text) if s and s.strip()]
    except Exception as exc:
        logger.error("_split_sentences nltk tokenize failed: {exc}", exc=exc)
    # Fallback: simple regex splitter with tiny heuristic to keep common abbreviations
    parts: List[str] = []
    start = 0
    for m in re.finditer(r'([.!?])\s+', text):
        end = m.end()
        frag = text[start:end].strip()
        if not frag:
            start = end
            continue
        if len(frag) <= 6 or frag[-3:].lower() in {'.  ', '.\n', '.\t'}:
            pass
        # suppress split on very short fragments likely to be abbreviations
        if len(frag) < 10 and any(frag.lower().endswith(a) for a in _ABBR):
            continue
        parts.append(frag)
        start = end
    tail = text[start:].strip()
    if tail:
        parts.append(tail)
    return parts


def chunk_text_sentences(text: str, *, max_chars: int = 1200, overlap: int = 120) -> List[Dict[str, Any]]:
    """Group sentences into chunks respecting sentence boundaries.

    Returns a list of { start, end, text } with char offsets into the original text.
    """
    sents = _split_sentences(text)
    out: List[Dict[str, Any]] = []
    if not sents:
        return out
    # reconstruct offsets
    offsets: List[int] = []
    pos = 0
    for s in sents:
        idx = text.find(s, pos)
        if idx < 0:
            idx = pos
        offsets.append(idx)
        pos = idx + len(s)

    i = 0
    while i < len(sents):
        start_idx = i
        chunk_text = sents[i]
        while (i + 1) < len(sents) and (len(chunk_text) + 1 + len(sents[i + 1])) <= max_chars:
            i += 1
            chunk_text += " " + sents[i]
        start_char = offsets[start_idx]
        end_char = start_char + len(chunk_text)
        out.append({ 'start': int(start_char), 'end': int(end_char), 'text': chunk_text })
        if overlap > 0 and i + 1 < len(sents):
            # step back by overlap proportion of chunk length via sentences
            back_chars = 0
            j = i
            while j > start_idx and back_chars < overlap:
                back_chars += len(sents[j]) + 1
                j -= 1
            i = max(j + 1, i + 1)
        else:
            i += 1
    return out

