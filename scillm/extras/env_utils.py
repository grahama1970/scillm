import os
import json
from pathlib import Path
from typing import Iterable, Sequence, Set, TypeVar, Callable, Any, Optional
from dotenv import load_dotenv, find_dotenv

# Use python-dotenv for .env support (no custom loader)
try:
    # Auto-load from CWD upwards; do not override existing env
    load_dotenv(find_dotenv(usecwd=True), override=False)
except Exception:
    pass

def _env_str(name: str, fallback: str = "") -> str:
    raw = os.getenv(name)
    return raw.strip() if raw else fallback

def _env_bool(name: str, fallback: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    v = raw.strip().lower()
    if v in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "f", "no", "n", "off"}:
        return False
    return fallback

def _env_list(
    name: str,
    fallback: Sequence[str] = (),
    sep: str = ",",
    strip_items: bool = True,
    allow_empty: bool = False,
) -> Sequence[str]:
    raw = os.getenv(name)
    if raw is None or (not raw.strip() and not allow_empty):
        return fallback
    parts = raw.split(sep)
    if strip_items:
        parts = [p.strip() for p in parts]
    return [p for p in parts if allow_empty or p]

def _env_set(name: str, fallback: Set[str] = frozenset(), sep: str = ",") -> Set[str]:
    return set(_env_list(name=name, fallback=fallback, sep=sep))

def _env_json(name: str, fallback: Any) -> Any:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        return json.loads(raw)
    except Exception:
        return fallback

def _env_required(name: str) -> str:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return raw.strip()

def _env_path(
    name: str,
    fallback: Optional[Path] = None,
    must_exist: bool = False,
    create_dir: bool = False,
) -> Optional[Path]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    p = Path(raw).expanduser()
    if must_exist and not p.exists():
        return fallback
    if create_dir and not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            return fallback
    return p

def _env_enum(name: str, allowed: Iterable[str], fallback: str) -> str:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    v = raw.strip()
    return v if v in allowed else fallback

def _env_seconds(name: str, fallback: float) -> float:
    """
    Supports simple duration suffixes:
    s (seconds), m (minutes), h (hours)
    Example: 30s, 2m, 1.5h
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    txt = raw.strip().lower()
    try:
        if txt.endswith("ms"):
            return float(txt[:-2]) / 1000.0
        if txt.endswith("s"):
            return float(txt[:-1])
        if txt.endswith("m"):
            return float(txt[:-1]) * 60.0
        if txt.endswith("h"):
            return float(txt[:-1]) * 3600.0
        return float(txt)
    except Exception:
        return fallback

_T = TypeVar("_T")

def _env_cast(name: str, cast: Callable[[str], _T], fallback: _T) -> _T:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    try:
        return cast(raw.strip())
    except Exception:
        return fallback
def _env_float(name: str, fallback: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return float(raw)
    except Exception:
        return fallback


def _env_int(name: str, fallback: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return fallback
    try:
        return int(float(raw))
    except Exception:
        return fallback