from __future__ import annotations

from pathlib import Path

from scillm.proxy.config import load_config


def _write_config(path: Path, api_base: str) -> None:
    path.write_text(
        f"""
model_list:
  - model_name: local-text
    scillm_params:
      model: qwen2.5:0.5b
      api_base: {api_base}
      api_key: ollama
      timeout: 20
""".lstrip()
    )


def test_load_config_rewrites_ollama_loopback_inside_docker(tmp_path, monkeypatch):
    config_path = tmp_path / "proxy_server_config.yaml"
    _write_config(config_path, "http://127.0.0.1:11434/v1")

    monkeypatch.setenv("SCILLM_RUNNING_IN_DOCKER", "1")
    monkeypatch.delenv("SCILLM_DOCKER_OLLAMA_BASE", raising=False)

    config = load_config(config_path)

    dep = config.model_groups["local-text"].deployments[0]
    assert dep.api_base == "http://ollama:11434/v1"
    assert config.ollama_api_base == "http://ollama:11434/v1"


def test_load_config_keeps_ollama_loopback_outside_docker(tmp_path, monkeypatch):
    config_path = tmp_path / "proxy_server_config.yaml"
    _write_config(config_path, "http://127.0.0.1:11434")

    monkeypatch.setenv("SCILLM_RUNNING_IN_DOCKER", "0")

    config = load_config(config_path)

    dep = config.model_groups["local-text"].deployments[0]
    assert dep.api_base == "http://127.0.0.1:11434/v1"
    assert config.ollama_api_base == "http://127.0.0.1:11434/v1"


def test_load_config_allows_custom_docker_ollama_base(tmp_path, monkeypatch):
    config_path = tmp_path / "proxy_server_config.yaml"
    _write_config(config_path, "http://localhost:11434")

    monkeypatch.setenv("SCILLM_RUNNING_IN_DOCKER", "1")
    monkeypatch.setenv("SCILLM_DOCKER_OLLAMA_BASE", "http://ollama-inference:11434")

    config = load_config(config_path)

    dep = config.model_groups["local-text"].deployments[0]
    assert dep.api_base == "http://ollama-inference:11434/v1"
    assert config.ollama_api_base == "http://ollama-inference:11434/v1"


def test_with_openai_v1_suffix_preserves_versioned_bases():
    """z.ai (/api/paas/v4) and other /vN bases must not get a second /v1.

    Regression: _with_openai_v1_suffix used to append /v1 to every base that
    did not already end in /v1, turning z.ai's /api/paas/v4 into the 404 path
    /api/paas/v4/v1/chat/completions. Bases whose path already ends in a /vN
    version segment are left alone; unversioned bases still get /v1.
    """
    from scillm.proxy.config import _with_openai_v1_suffix as suffix

    # /vN version already in the path -> unchanged
    assert suffix("https://api.z.ai/api/paas/v4") == "https://api.z.ai/api/paas/v4"
    assert suffix("https://api.z.ai/api/coding/paas/v4") == "https://api.z.ai/api/coding/paas/v4"
    assert suffix("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
    assert suffix("http://127.0.0.1:11434/v1/") == "http://127.0.0.1:11434/v1"
    # no version segment -> /v1 appended (unchanged behavior)
    assert suffix("https://api.openai.com") == "https://api.openai.com/v1"
    assert (
        suffix("https://generativelanguage.googleapis.com/v1beta/openai")
        == "https://generativelanguage.googleapis.com/v1beta/openai/v1"
    )
