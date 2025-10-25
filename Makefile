# LiteLLM Makefile
# Simple Makefile for running tests and basic development tasks

.PHONY: help test test-unit test-integration test-unit-helm lint format install-dev install-proxy-dev install-test-deps install-helm-unittest check-circular-imports check-import-safety run-scenarios run-stress-tests project-ready project-ready-live project-ready-summary

# Default target
help:
	@echo "Available commands:"
	@echo "  make install-dev        - Install development dependencies"
	@echo "  make install-proxy-dev  - Install proxy development dependencies"
	@echo "  make install-dev-ci     - Install dev dependencies (CI-compatible, pins OpenAI)"
	@echo "  make install-proxy-dev-ci - Install proxy dev dependencies (CI-compatible)"
	@echo "  make install-test-deps  - Install test dependencies"
	@echo "  make install-helm-unittest - Install helm unittest plugin"
	@echo "  make format             - Apply Black code formatting"
	@echo "  make format-check       - Check Black code formatting (matches CI)"
	@echo "  make lint               - Run all linting (Ruff, MyPy, Black check, circular imports, import safety)"
	@echo "  make run-scenarios      - Run live scenarios (mini-agent, router demos, chutes, codex-agent)"
	@echo "  make lean4-bridge       - Start Lean4 bridge on :8787"
	@echo "  make lean4-live        - Probe Lean4 bridge (live scenario)"
	@echo "  make codeworld-bridge   - Start CodeWorld bridge on :8887"
	@echo "  make codeworld-live    - Probe CodeWorld bridge (live scenario)"
	@echo "  make run-stress-tests   - Run live stress scenarios (throughput, bursts, codex, mini-agent)"
	@echo "  make lint-ruff          - Run Ruff linting only"
	@echo "  make lint-mypy          - Run MyPy type checking only"
	@echo "  make lint-black         - Check Black formatting (matches CI)"
	@echo "  make check-circular-imports - Check for circular imports"
	@echo "  make check-import-safety - Check import safety"
	@echo "  make test               - Run all tests"
	@echo "  make test-unit          - Run unit tests (tests/test_litellm)"
	@echo "  make test-integration   - Run integration tests"
	@echo "  make test-unit-helm     - Run helm unit tests"
	@echo "  make review-bundle      - Create standard code review bundle (Markdown)"
	@echo "  make review-bundle-custom - Create custom ==== FILE style review bundle"
	@echo "  make review-bundle-gist FILE=... - Upload a file as a private GitHub Gist (requires GITHUB_TOKEN)"
	@echo "  make project-ready        - Run deterministic + local readiness (no network)"
	@echo "  make project-ready-live   - Run strict/live readiness (enforces required providers)"
	@echo "  make project-ready-summary- Print last readiness PASS/FAIL/SKIP summary"
	@echo "  make review-run           - Run codex-agent review + comparison (uses mini-agent by default)"
	@echo "  make codex-agent-doctor   - Check codex-agent health, /v1/models, and a high-reasoning ping"
	@echo "  make codeworld-bridge-up-only   - Start CodeWorld bridge only on :8887 (no Redis)"
	@echo "  make codeworld-bridge-down-only - Stop CodeWorld bridge-only container"
	@echo "  make mcts-live              - Run live MCTS (:auto) end-to-end via codex-agent + CodeWorld"
	@echo "  make model-alias-doctor   - Resolve doc-style -> canonical model id (NAME=...)"
	@echo "  make json-reliability-doctor - Probe models for strict JSON (MODELS=... or FILE=path)"

# --- Logo exports -------------------------------------------------------------
.PHONY: logo-export
logo-export:
	@echo "Exporting outlined SVG variants (requires Inkscape >= 1.0)"
	@which inkscape >/dev/null 2>&1 || (echo "Inkscape not found. Install it to outline text." && exit 1)
	@mkdir -p local/artifacts/logo
	inkscape SciLLM_friendly.svg --export-plain-svg=local/artifacts/logo/SciLLM_friendly_outlined.svg --export-text-to-path
	inkscape SciLLM_balanced.svg --export-plain-svg=local/artifacts/logo/SciLLM_balanced_outlined.svg --export-text-to-path
	@echo "Exporting PNG favicons"
	inkscape SciLLM_icon.svg --export-filename=local/artifacts/logo/favicon-32.png -w 32 -h 32
	inkscape SciLLM_icon.svg --export-filename=local/artifacts/logo/favicon-180.png -w 180 -h 180
	@echo "Attempting to export ICO favicon (requires ImageMagick 'convert')"
	@if command -v convert >/dev/null 2>&1; then \
	  convert local/artifacts/logo/favicon-32.png local/artifacts/logo/favicon-180.png local/artifacts/logo/favicon.ico; \
	  echo "Wrote local/artifacts/logo/favicon.ico"; \
	else \
	  echo "ImageMagick not found. To make favicon.ico: convert favicon-32.png favicon-180.png favicon.ico"; \
	fi
	@echo "Done. See local/artifacts/logo/."

# Installation targets
install-dev:
	uv sync --group dev

install-proxy-dev:
	uv sync --group dev --group proxy-dev --extra proxy

# CI-compatible installations (matches GitHub workflows exactly)
install-dev-ci:
	uv sync --group dev

install-proxy-dev-ci:
	uv sync --group dev --group proxy-dev --extra proxy

install-test-deps: install-proxy-dev
	uv run pip install "pytest-retry==1.6.3" pytest-xdist
	cd enterprise && uv pip install -e . && cd ..

install-helm-unittest:
	helm plugin install https://github.com/helm-unittest/helm-unittest --version v0.4.4 || echo "ignore error if plugin exists"

# Formatting
format: install-dev
	cd litellm && uv run black . && cd ..

format-check: install-dev
	cd litellm && uv run black --check . && cd ..

# Linting targets
lint-ruff: install-dev
	cd litellm && uv run ruff check . && cd ..

lint-mypy: install-dev
	uv run pip install types-requests types-setuptools types-redis types-PyYAML
	cd litellm && uv run mypy . --ignore-missing-imports && cd ..

lint-black: format-check

	run-scenarios:
		@. .venv/bin/activate && python scenarios/run_all.py

lean4-live:
	PYTHONPATH=$(PWD) python scenarios/lean4_bridge_release.py

lean4-bridge:
	PYTHONPATH=src uvicorn lean4_prover.bridge.server:app --port 8787 --log-level warning

codeworld-bridge:
	PYTHONPATH=src uvicorn codeworld.bridge.server:app --port 8888 --log-level warning

codeworld-live:
	PYTHONPATH=$(PWD) python scenarios/codeworld_bridge_release.py

.PHONY: codeworld-bridge-up-only codeworld-bridge-down-only
codeworld-bridge-up-only:
	docker compose -f local/docker/compose.codeworld.bridge.yml up -d --build
	@echo "Waiting 2s for health..." && sleep 2
	@curl -sSf http://127.0.0.1:8888/healthz || true

codeworld-bridge-down-only:
	docker compose -f local/docker/compose.codeworld.bridge.yml down

.PHONY: mcts-live
mcts-live:
	@if ! curl -sf $${CODEX_AGENT_API_BASE:-http://127.0.0.1:8089}/healthz >/dev/null; then echo "codex-agent not healthy; set CODEX_AGENT_API_BASE and start sidecar" && exit 2; fi
	@if ! curl -sf $${CODEWORLD_BASE:-http://127.0.0.1:8888}/healthz >/dev/null; then echo "CodeWorld bridge not healthy; run 'make codeworld-bridge-up-only'" && exit 2; fi
	@echo "Running live MCTS (:auto) with codex-agent=$${CODEX_AGENT_API_BASE:-http://127.0.0.1:8089} and codeworld=$${CODEWORLD_BASE:-http://127.0.0.1:8888}"
	@python scenarios/mcts_codeworld_auto_release.py

run-stress-tests:
	@echo "Running throughput benchmark"
	@. .venv/bin/activate && python stress_tests/parallel_throughput_benchmark.py
	@echo "Running parallel burst test"
	@. .venv/bin/activate && python stress_tests/parallel_acompletions_burst.py
	@echo "Running codex-agent rate limit test"
	@. .venv/bin/activate && python stress_tests/codex_agent_rate_limit_backoff.py
	@echo "Running mini-agent concurrency test"
	@. .venv/bin/activate && python stress_tests/mini_agent_concurrency.py

.PHONY: codex-agent-doctor
codex-agent-doctor:
	uv run python scripts/doctor/codex_agent_doctor.py

.PHONY: codeworld-bridge-doctor

.PHONY: model-alias-doctor
model-alias-doctor:
	@if [ -z "$$NAME" ]; then echo "Usage: NAME='<requested_model>' make model-alias-doctor"; exit 2; fi
	uv run python scripts/doctor/model_alias_doctor.py --name "$$NAME"

codeworld-bridge-doctor:
	@curl -sSf $${CODEWORLD_BASE:-http://127.0.0.1:8888}/healthz | jq . || (echo "Bridge not healthy at $${CODEWORLD_BASE:-http://127.0.0.1:8888}"; exit 2)

check-circular-imports: install-dev
	cd litellm && uv run python ../tests/documentation_tests/test_circular_imports.py && cd ..

check-import-safety: install-dev
	uv run python -c "from litellm import *" || (echo '🚨 import failed, this means you introduced unprotected imports! 🚨'; exit 1)

# Combined linting (matches test-linting.yml workflow)
lint: format-check lint-ruff lint-mypy check-circular-imports check-import-safety

# Testing targets
test:
	uv run pytest tests/

test-unit: install-test-deps
	uv run pytest tests/test_litellm -x -vv -n 4

test-integration:
	uv run pytest tests/ -k "not test_litellm"

test-unit-helm: install-helm-unittest
	helm unittest -f 'tests/*.yaml' deploy/charts/litellm-helm

# LLM Translation testing targets
test-llm-translation: install-test-deps
	@echo "Running LLM translation tests..."
	@uv run python .github/workflows/run_llm_translation_tests.py

test-llm-translation-single: install-test-deps
	@echo "Running single LLM translation test file..."
	@if [ -z "$(FILE)" ]; then echo "Usage: make test-llm-translation-single FILE=test_filename.py"; exit 1; fi
	@mkdir -p test-results
	uv run pytest tests/llm_translation/$(FILE) \
		--junitxml=test-results/junit.xml \
		-v --tb=short --maxfail=100 --timeout=300

canary-run:
	@echo "Running single parity check"
	PYTHONPATH=$(PWD) python local/scripts/router_core_parity.py

canary-summarize:
	@echo "Summarizing parity JSONL"
	PYTHONPATH=$(PWD) python local/scripts/parity_summarize.py --in $${PARITY_OUT}

.PHONY: exec-rpc-up exec-rpc-restart exec-rpc-down exec-rpc-logs exec-rpc-probe

# Exec RPC service (Dockerized). Rebuilds from local tree and (re)starts on ${EXEC_RPC_PORT:-8790}.
exec-rpc-up:
	docker compose -f local/docker/compose.exec.yml up -d --build

exec-rpc-restart:
	docker compose -f local/docker/compose.exec.yml up -d --build

exec-rpc-down:
	docker compose -f local/docker/compose.exec.yml down

exec-rpc-logs:
	docker compose -f local/docker/compose.exec.yml logs -f exec-rpc

# Probe: health + python exec must include t_ms
exec-rpc-probe:
	./scripts/exec_rpc_probe.sh 127.0.0.1 $${EXEC_RPC_PORT:-8790} || true

.PHONY: review-bundle review-bundle-custom

review-bundle:
	@mkdir -p local/artifacts/review
	python local/scripts/review_bundle.py \
	  --files-from local/scripts/review/files.txt \
	  --prefix-file local/scripts/review/persona_and_rubric.md \
	  --output local/artifacts/review/review_bundle.md \
	  --single-file --token-estimator char || true

review-bundle-custom:
	@mkdir -p local/artifacts/review
	CONTEXT=local/artifacts/review/context_preface.txt OUT=local/artifacts/review/review_bundle.txt \
	python local/scripts/review/make_custom_bundle.py

.PHONY: review-bundle-gist
review-bundle-gist:
	@if [ -z "$(FILE)" ]; then echo "Usage: make review-bundle-gist FILE=/abs/path/to/REVIEW_BUNDLE_PROMPT.md"; exit 2; fi
	@if [ -z "$$GITHUB_TOKEN" ]; then echo "Set GITHUB_TOKEN with 'gist' scope"; exit 2; fi
	@python local/scripts/review/make_gist.py --file "$(FILE)"

.PHONY: e2e-up e2e-run e2e-down

# Bring up live services required for E2E (best-effort)
e2e-up: exec-rpc-up
	@echo "If needed, start the mini-agent app:"
	@echo "  uvicorn litellm.experimental_mcp_client.mini_agent.agent_proxy:app --host 0.0.0.0 --port 8788"

e2e-run:

e2e-down: exec-rpc-down

# --- Dockerized mini-agent helpers -------------------------------------------

docker-up:
	@docker network create llmnet >/dev/null 2>&1 || true
	API_PORT=$${API_PORT:-8788} API_CONTAINER_PORT=$${API_CONTAINER_PORT:-8788} \
		docker compose -f local/docker/compose.exec.yml up -d --build $${OLLAMA:+ollama} tools-stub agent-api
	@echo "Mini-agent API: http://127.0.0.1:$${API_PORT:-8788} (GET /ready)"

docker-down:
	@docker compose -f local/docker/compose.exec.yml down || true

docker-logs:
	@docker compose -f local/docker/compose.exec.yml logs -f agent-api

# Optional: bring up an Ollama daemon attached to llmnet for model inference
docker-ollama-up:
	@docker network create llmnet >/dev/null 2>&1 || true
	@docker run -d --name ollama --network llmnet -p 11434:11434 ollama/ollama || true
	@echo "Ollama base URL (host): $$([ -x scripts/resolve_ollama_base.sh ] && scripts/resolve_ollama_base.sh || echo http://127.0.0.1:11434)"

docker-ollama-down:
	@docker rm -f ollama >/dev/null 2>&1 || true

	DOCKER_MINI_AGENT=1 \
	MINI_AGENT_API_HOST=$${MINI_AGENT_API_HOST:-127.0.0.1} \
	MINI_AGENT_API_PORT=$${MINI_AGENT_API_PORT:-8788} \
	LITELLM_ENABLE_CODEX_AGENT=1 \
	CODEX_AGENT_API_BASE=$${CODEX_AGENT_API_BASE:-http://127.0.0.1:8788} \
	LITELLM_DEFAULT_CODE_MODEL=$${LITELLM_DEFAULT_CODE_MODEL:-codex-agent/mini} \
	PYTHONPATH=$(PWD) python -m pytest -q \
	  -q || true

	DOCKER_MINI_AGENT=1 \
	MINI_AGENT_API_HOST=$${MINI_AGENT_API_HOST:-127.0.0.1} \
	MINI_AGENT_API_PORT=$${MINI_AGENT_API_PORT:-8788} \

	/bin/sh -lc '. local/scripts/anti_drift_preflight.sh; python scripts/mvp_check.py' || true

	/bin/sh -lc '. local/scripts/anti_drift_preflight.sh; READINESS_LIVE=1 STRICT_READY=1 READINESS_EXPECT=ollama,codex-agent,docker DOCKER_MINI_AGENT=1 python scripts/mvp_check.py' || true
	python scripts/generate_project_ready.py || true

	@if [ ! -f local/artifacts/mvp/mvp_report.json ]; then \
	else \
	  jq -r '.checks[] | [.name,(.ok|tostring), (if has("skipped") then (.skipped|tostring) else "" end)] | @tsv' local/artifacts/mvp/mvp_report.json \
	    | awk 'BEGIN{FS="\t"} {em=$$2=="true"?"✅":($$3=="true"?"⏭":"❌"); printf("%-26s %s\n", $$1, em)}'; \
	fi

.PHONY: dump-readiness-env
dump-readiness-env:
	@echo "STRICT_READY=$${STRICT_READY:-0} READINESS_LIVE=$${READINESS_LIVE:-0} READINESS_EXPECT=$${READINESS_EXPECT:-} DOCKER_MINI_AGENT=$${DOCKER_MINI_AGENT:-0}"
	@echo "MINI_AGENT_API_HOST=$${MINI_AGENT_API_HOST:-127.0.0.1} MINI_AGENT_API_PORT=$${MINI_AGENT_API_PORT:-8788}"
	@echo "CODEX_AGENT_API_BASE=$${CODEX_AGENT_API_BASE:-auto} OLLAMA_API_BASE=$${OLLAMA_API_BASE:-http://127.0.0.1:11434}"

	# Strict gate: split checks only (core + ND). Docker optional.

	OLLAMA_API_BASE=$${OLLAMA_API_BASE:-http://127.0.0.1:11434} \
	READINESS_LIVE=1 STRICT_READY=1 \

	@python scripts/print_ready_summary.py
# Bridges: CodeWorld & Lean4
STACK_COMPOSE ?= deploy/docker/compose.scillm.stack.yml

.PHONY: bridge-up bridge-down bridge-restart bridge-watch codeworld-smoke lean4-smoke codex-regression

bridge-up:
	@docker compose -f $(STACK_COMPOSE) up -d codeworld-bridge lean4-bridge
	@echo "Bridges started. Health:"
	@curl -sSf http://127.0.0.1:8887/healthz || true
	@curl -sSf http://127.0.0.1:8787/healthz || true

bridge-down:
	@docker compose -f $(STACK_COMPOSE) rm -sf codeworld-bridge lean4-bridge || true

bridge-restart:
	@$(MAKE) bridge-down || true
	@$(MAKE) bridge-up

# -----------------------------------------------------------------------------
# Project Readiness — One Way to Green
# -----------------------------------------------------------------------------

.PHONY: project-ready
project-ready:
	/bin/sh -lc '. local/scripts/anti_drift_preflight.sh; python scripts/mvp_check.py' || true
	python scripts/generate_project_ready.py || true
	@$(MAKE) project-ready-summary || true

.PHONY: project-ready-live
project-ready-live:
	/bin/sh -lc '. local/scripts/anti_drift_preflight.sh; READINESS_LIVE=1 STRICT_READY=1 python scripts/mvp_check.py' || true
	python scripts/generate_project_ready.py || true
	@$(MAKE) project-ready-summary || true

.PHONY: project-ready-summary
project-ready-summary:
	@python scripts/print_ready_summary.py || true

.PHONY: project-agent-doctor
project-agent-doctor:
	@chmod +x scripts/project_agent_doctor.zsh || true
	@./scripts/project_agent_doctor.zsh

.PHONY: review-run
review-run:
	@chmod +x scripts/review/run_review_e2e.zsh || true
	@DRY_RUN?=0 scripts/review/run_review_e2e.zsh

bridge-watch:
	@python scripts/watch_bridges.py --loop 30

codeworld-smoke:
	@PYTHONPATH=$(PWD) python scenarios/codeworld_bridge_release.py

lean4-smoke:
	@PYTHONPATH=$(PWD) LEAN4_BRIDGE_ECHO?=1 python scenarios/lean4_bridge_release.py

codex-regression:
	@python scenarios/codex_agent_regression_check.py


.PHONY: json-reliability-doctor
json-reliability-doctor:
	@if [ -z "$$MODELS" ] && [ -z "$$FILE" ]; then echo "Usage: MODELS='a,b,c' make json-reliability-doctor  OR  FILE=path make json-reliability-doctor"; exit 2; fi
	uv run python scripts/doctor/json_reliability_doctor.py \
	  $${MODELS:+--models "$$MODELS"} \
	  $${FILE:+--models-file "$$FILE"} \
	  $${BASE:+--base "$$BASE"} \
	  $${KEY:+--key "$$KEY"} \
	  $${CUTOFF:+--cutoff "$$CUTOFF"} \
	  --print-table
## Codex Cloud targets removed (deprecated/disabled). See QUICKSTART.md.
CERTAINLY_COMPOSE?=docker/compose.certainly.bridge.yml
SCILLM_BRIDGES_PROJECT?=scillm-bridges

.PHONY: certainly-bridge-up
certainly-bridge-up:
	@echo "[certainly] bringing up container via $${CERTAINLY_COMPOSE}"
	@if curl -fsS http://127.0.0.1:8791/healthz >/dev/null; then \
	  echo "[certainly] already healthy on 8791"; \
	else \
	  docker compose -p $(SCILLM_BRIDGES_PROJECT) -f $(CERTAINLY_COMPOSE) up -d; \
	fi
	@echo "[certainly] waiting for healthz on http://127.0.0.1:8791/healthz"
	@bash -c 'for i in 1 2 3 4 5; do curl -fsS http://127.0.0.1:8791/healthz >/dev/null && echo "ok" && exit 0; sleep 1; done; echo "timeout waiting for healthz" && exit 1'

.PHONY: certainly-bridge-down
certainly-bridge-down:
	@echo "[certainly] stopping container via $${CERTAINLY_COMPOSE}"
	docker compose -p $(SCILLM_BRIDGES_PROJECT) -f $(CERTAINLY_COMPOSE) down -v || true

.PHONY: certainly-bridge-doctor
certainly-bridge-doctor:
	@bash debug/certainly_bridge_doctor.sh
stability:
	@echo "[stability] running SciLLM stability check (all components)…"
	@PYTHONPATH=src:$(PWD) python scripts/scillm_stability_check.py --all || true
# Chutes host doctor: poll -> single chat -> small batch
.PHONY: chutes-host-doctor
chutes-host-doctor:
	@CHUTES_API_KEY=$(CHUTES_API_KEY) \
	SLUG=$(SLUG) \
	PYTHONPATH=src:. \
	python scripts/chutes_host_doctor.py --slug "$${SLUG}" --model "$${MODEL}" --concurrency $${CONCURRENCY:-2}
notebooks-smoke:
	@echo "[smokes] running feature smokes (no notebook logic)"
	@env SCILLM_FORCE_HTTPX_STREAM=1 $$(grep -v '^#' .env | xargs) uv run -- python scripts/feature_smokes.py
	@echo "[notebooks] generating viewer notebooks"
	@uv run -- python scripts/notebooks_build.py
	@echo "[smokes] summaries:"
	@-test -f .artifacts/nb_chutes_openai_compatible.json && echo "  - chutes_openai: $$(cat .artifacts/nb_chutes_openai_compatible.json)" || true
	@-test -f .artifacts/nb_router_parallel_batch.json && echo "  - router_parallel: $$(cat .artifacts/nb_router_parallel_batch.json)" || true
	@-test -f .artifacts/nb_model_list_first_success.json && echo "  - model_list: $$(cat .artifacts/nb_model_list_first_success.json)" || true

.PHONY: warmup-probe
warmup-probe:
	@echo "[chutes] warmup probe"
	@env $$(grep -v '^#' .env | xargs) PYTHONPATH=src:. uv run -- python scripts/chutes_warmup_probe.py --wait-seconds $${WAIT_SECONDS:-180}
