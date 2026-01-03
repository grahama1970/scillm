## Repo Layout (SciLLM)

This repository is a fork of LiteLLM with additional SciLLM surfaces and tooling. To keep it workable, we keep the repo root small and put “human docs”, runbooks, and one-off investigation notes under `docs/` (or `local/` if they are machine-generated / private).

### Keep At Root (expected by tooling)

These files commonly have implicit consumers (CI, Docker, GitHub conventions, package tooling). Prefer keeping them at repo root:

- Build / packaging: `pyproject.toml`, `uv.lock`, `requirements.txt`, `poetry.lock`
- CI + formatting: `.github/`, `.gitignore`, `.pre-commit-config.yaml`, `pytest.ini`, `ruff.toml`, `.flake8`, `codecov.yaml`, `pyrightconfig.json`
- Containers / deploy: `Dockerfile`, `docker-compose.yml`, `render.yaml`
- Top-level project metadata: `README.md`, `LICENSE`, `CHANGELOG.md`, `CONTRIBUTING.md`, `security.md`, `AGENTS.md`
- Provider catalogs (consumed by runtime): `model_prices_and_context_window.json`, `schema.prisma`, `index.yaml`

### Docs

- SciLLM docs live in `docs/scillm/` (quickstart, envs, contracts, runbooks).
- Provider / architecture docs live under `docs/` and `docs/my-website/`.
- Agent/copilot prompts and review templates live in `docs/agents/`.
- Investigation notes and reproducible bug writeups belong in `docs/issues/`.

### Scripts

- Reusable scripts belong in `scripts/`.
- Scenario/demo scripts belong in `scenarios/`.
- Anything that only works in a specific sibling repo should not live here (move it to that repo).

### Local Artifacts (don’t commit)

Use `local/` for:

- run artifacts, cache, JSONL logs, and any outputs that should not be committed
- per-developer config files (prefer `*.example` for tracked templates)

Examples:
- MCP config template: `local/mcp_servers.json.example` (copy to `local/mcp_servers.json`)
- Prometheus example config: `deploy/observability/prometheus.yml`

### Rules of Thumb

- If you’re adding a new doc: put it in `docs/scillm/` (or `docs/agents/` / `docs/issues/`).
- If you’re adding a new config: prefer `local/*.example` for templates and document the path in the relevant doc.
- Avoid adding new files at the repo root unless a tool requires it.

Guardrail:
- `make check-root-layout` enforces a small allowlist of tracked root files.
