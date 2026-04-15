from __future__ import annotations
import os
import sys
from typing import Dict
from loguru import logger

def _load_dotenv_from_repo_root() -> None:
    """Best-effort: load a .env file from the repository tree.
    - Walk up from this file to parents to find the first '.env'.
    - Fall back to CWD '.env' if none found.
    - No override of already-set env vars.
    """
    try:
        from pathlib import Path
        try:
            from dotenv import load_dotenv  # type: ignore
        except Exception as exc:
            logger.error("Suppressed error in env: {}", exc)
            return  # python-dotenv not installed; skip silently

        start = Path(__file__).resolve()
        env_path = None
        # search current file's parents
        for p in [start] + list(start.parents):
            cand = p / ".env"
            if cand.exists():
                env_path = cand
                break
        if env_path is None:
            # fallback to cwd
            cwd = Path.cwd() / ".env"
            if cwd.exists():
                env_path = cwd
        if env_path is not None:
            load_dotenv(env_path, override=False)
    except Exception as exc:
        logger.error("Suppressed error in env: {}", exc)
        # never fail due to dotenv
        pass


def _normalize_arango_env() -> None:
    """Canonicalize Arango credentials env.
    - Prefer ARANGO_PASS; accept ARANGO_PASSWORD as an alias if ARANGO_PASS is unset.
    - If both are set and differ, keep ARANGO_PASS and warn once.
    """
    ap = os.getenv("ARANGO_PASS")
    alias = os.getenv("ARANGO_PASSWORD")
    if not ap and alias:
        os.environ["ARANGO_PASS"] = alias
    elif ap and alias and ap != alias:
        print(
            "[graph-memory] WARNING: Both ARANGO_PASS and ARANGO_PASSWORD are set; "
            "preferring ARANGO_PASS and ignoring ARANGO_PASSWORD.",
            file=sys.stderr,
        )


def arango_cfg() -> Dict[str, str]:
    """Return normalized Arango configuration from environment.

    Keys returned: url, db, user, password.
    """
    _load_dotenv_from_repo_root()
    _normalize_arango_env()
    url = os.getenv("ARANGO_URL") or f"http://{os.getenv('ARANGO_HOST','127.0.0.1')}:{os.getenv('ARANGO_PORT','8529')}"
    db = os.getenv("ARANGO_DB") or os.getenv("ARANGO_DATABASE", "memory")
    user = os.getenv("ARANGO_USER", "root")
    password = os.getenv("ARANGO_PASS", "")
    if password == "":
        print("[graph-memory] WARNING: ARANGO_PASS is empty; Arango auth will likely fail (401).", file=sys.stderr)
    return {"url": url, "db": db, "user": user, "password": password}


def embedding_url() -> str:
    """Return embedding service URL. Docker uses service name, local uses localhost."""
    return os.getenv("EMBEDDING_SERVICE_URL", "http://127.0.0.1:8602")


def vector_store_url() -> str:
    """Return vector store URL. Docker uses service name, local uses localhost."""
    return os.getenv("VECTOR_STORE_URL", "http://127.0.0.1:8600")


def scillm_url() -> str:
    """Return scillm service URL. Default port is 4001."""
    return os.getenv("SCILLM_URL", "http://127.0.0.1:4001")
