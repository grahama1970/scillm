import os
from pathlib import Path
from loguru import logger

def _get_port_from_file(skill_name: str, default_port: int) -> int:
    """Read .port file from skill directory if available.

    Services dynamically find an open port and write it to .port file.
    We search multiple locations to find the active service port.
    """
    try:
        home = Path.home()
        candidates = [
            # Primary: pi-mono workspace (where services typically run)
            home / "workspace" / "experiments" / "pi-mono" / ".pi" / "skills" / skill_name / ".port",
            # User's ~/.pi/skills (if installed there)
            home / ".pi" / "skills" / skill_name / ".port",
            # Relative to cwd (for pi-mono dev)
            Path(os.getcwd()) / ".pi" / "skills" / skill_name / ".port",
            # Memory project's own skills
            Path(__file__).parent.parent.parent / ".agents" / "skills" / skill_name / ".port",
        ]

        for p in candidates:
            if p.exists():
                port = int(p.read_text().strip())
                return port
    except Exception as exc:
        logger.error("Suppressed error in config: {}", exc)
    return default_port

def get_service_url(env_var: str, skill_name: str, default_port: int) -> str:
    """Resolve service URL from env or dynamic port file."""
    url = os.getenv(env_var)
    if url:
        return url
    
    port = _get_port_from_file(skill_name, default_port)
    return f"http://127.0.0.1:{port}"

# Exposed Configuration
EMBEDDING_SERVICE_URL = get_service_url("EMBEDDING_SERVICE_URL", "embedding", 8602)
VECTOR_SERVICE_URL = get_service_url("VECTOR_SERVICE_URL", "vector-store", 8600)
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "384"))

# Lessons collection name — lessons_v2 is the clean collection (161K docs),
# original "lessons" (372K) kept as backup. Change this to revert.
LESSONS_COLLECTION = os.environ.get("LESSONS_COLLECTION", "lessons_v2")
