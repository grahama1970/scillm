from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "docker" / "compose.scillm.core.yml"


def test_scillm_proxy_can_validate_host_workspace_cwd() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    volumes = compose["services"]["scillm-proxy"]["volumes"]

    assert "${HOME}/workspace:/home/graham/workspace:ro" in volumes


def test_opencode_serve_and_proxy_share_workspace_path_namespace() -> None:
    compose = yaml.safe_load(COMPOSE.read_text())
    services = compose["services"]

    proxy_volumes = services["scillm-proxy"]["volumes"]
    serve_volumes = services["opencode-serve"]["volumes"]

    assert "${HOME}/workspace:/home/graham/workspace:ro" in proxy_volumes
    assert "${HOME}/workspace:/home/graham/workspace" in serve_volumes
