Lessons learned (codex-agent / mini-agent)
- Mini-agent (Docker) healthy on 127.0.0.1:8788: /ready and /agent/run OK (debug/verify_mini_agent.py).
- Codex sidecar (Docker) healthy on 127.0.0.1:8077: /healthz OK; /v1/chat/completions returns string content (echo mode) (debug/verify_codex_agent_docker.py).
- Sidecar auth: /root/.codex/auth.json missing in container; echo mode hides it. For non-echo, mount ${HOME}/.codex/auth.json:ro or set CODEX_AUTH_PATH.
- Router ad-hoc requires LITELLM_ENABLE_CODEX_AGENT=1 set before import; per-item kwargs in parallel required: custom_llm_provider, api_base, api_key.
- Parallel codex echo probe currently returns provider_error and content=None via Router.parallel_acompletions when using ad-hoc kwargs; normalized dict includes scillm_router. Direct HTTP works and is acceptable fallback for devops batch.
- Docs updated with base rule (omit /v1), debug scripts, and auth guidance; new script debug/codex_parallel_probe.py added.
