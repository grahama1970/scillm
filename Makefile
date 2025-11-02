notebooks-smoke:
	@echo "Rebuilding viewer notebooks"
	@uv run -- python scripts/notebooks_build.py
	@echo "Running self-contained notebooks with 150s timeout each"
	@mkdir -p notebooks/executed
	@set -e; \
	for nb in \
	  notebooks/01_chutes_openai_compatible.ipynb \
	  notebooks/02_router_parallel_batch.ipynb \
	  notebooks/03_model_list_first_success.ipynb \
	  notebooks/04a_tools_only.ipynb \
	  notebooks/09_fallback_infer_with_meta.ipynb \
	  notebooks/10_auto_router_one_liner.ipynb \
	  notebooks/11_provider_perplexity.ipynb \
	  notebooks/14_provider_matrix.ipynb; do \
	  echo "Executing $$nb"; \
	  uv run -- python -m nbconvert --ExecutePreprocessor.timeout=150 --to notebook --execute $$nb --output $$(basename $$nb .ipynb)_executed.ipynb --output-dir notebooks/executed; \
	done
	@echo "OK"

notebooks-smoke-ci:
	@echo "CI smoke (subset that skips providers without keys)"
	@uv run -- python scripts/notebooks_build.py
	@mkdir -p notebooks/executed
	@OPENAI_API_KEY="" ANTHROPIC_API_KEY="" PERPLEXITY_API_KEY="" uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/14_provider_matrix.ipynb --output notebooks/executed/14_provider_matrix_executed.ipynb
	@uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/11_provider_perplexity.ipynb --output notebooks/executed/11_provider_perplexity_executed.ipynb
	@uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/09_fallback_infer_with_meta.ipynb --output notebooks/executed/09_fallback_infer_with_meta_executed.ipynb
	@uv run -- python -m nbconvert --ExecutePreprocessor.timeout=90 --to notebook --execute notebooks/10_auto_router_one_liner.ipynb --output notebooks/executed/10_auto_router_one_liner_executed.ipynb
	@echo "CI OK"

agents-smoke:
	@echo "Running agents/bridges notebooks with 180s timeout each"
	@mkdir -p notebooks/executed
	@set -e; \
	for nb in \
	  notebooks/05_codex_agent_doctor.ipynb \
	  notebooks/06_mini_agent_doctor.ipynb \
	  notebooks/07_codeworld_mcts.ipynb \
	  notebooks/08_certainly_bridge.ipynb; do \
	  echo "Executing $$nb"; \
	  uv run -- python -m nbconvert --ExecutePreprocessor.timeout=180 --to notebook --execute $$nb --output $$(basename $$nb .ipynb)_executed.ipynb --output-dir notebooks/executed; \
	done
	@echo "Agents OK"

# One-shot live doctor for Chutes (skips if env missing)
chutes-doctor:
	@if [ -z "$$CHUTES_API_BASE" ] || [ -z "$$CHUTES_API_KEY" ] || [ -z "$$CHUTES_TEXT_MODEL" ]; then \
	  echo "skip: set CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_TEXT_MODEL"; \
	  exit 0; \
	fi; \
	python scripts/chutes_doctor.py

# Guard: ensure logs do not contain raw Authorization/x-api-key values
check-no-secrets-logs:
	@! rg -n "Authorization:\s*Bearer\s+\w" -S . || (echo "Found Authorization token in repo logs/text" && exit 1)
	@! rg -n "x-api-key:\s*\w" -S . || (echo "Found x-api-key in repo logs/text" && exit 1)
grafana-import:
	@if [ -z "$$GRAFANA_URL" ] || [ -z "$$GRAFANA_TOKEN" ]; then \
	  echo "Usage: GRAFANA_URL=... GRAFANA_TOKEN=... make grafana-import"; \
	  exit 2; \
	fi
	python3 scripts/grafana_import_dashboards.py
proxy-run-uv:
	@if ! command -v uv >/dev/null 2>&1; then echo "uv not installed"; exit 2; fi
	@if [ ! -f local/proxy_server_config.yaml ]; then echo "missing local/proxy_server_config.yaml"; exit 2; fi
	LITELLM_MASTER_KEY=$${LITELLM_MASTER_KEY:-sk-dev-proxy-123} METRICS_ENV=$${METRICS_ENV:-dev} \
	CHUTES_PRICING_FILE=$${CHUTES_PRICING_FILE:-local/pricing/chutes.prices.json} \
	uv run litellm --config local/proxy_server_config.yaml --host 0.0.0.0 --port $${PORT:-4010} --log_level warning

prom-run-docker:
	@cat >/tmp/prom.yml <<'YAML'
global:
  scrape_interval: 15s
scrape_configs:
- job_name: litellm
  static_configs:
  - targets: ['host.docker.internal:$${PORT:-4010}']
YAML
	@docker rm -f scillm-prom >/dev/null 2>&1 || true
	docker run -d --name scillm-prom --add-host=host.docker.internal:host-gateway -p 9090:9090 -v /tmp/prom.yml:/etc/prometheus/prometheus.yml prom/prometheus:latest
	@echo "Prometheus listening on :9090"

compose-up:
	@echo "Starting SCILLM stack via compose (proxy+db+prom+grafana+ollama)"
	@docker compose -f local/docker/compose.scillm.yml up -d
	@echo "Waiting 5s for services to settle..."; sleep 5
	@docker compose -f local/docker/compose.scillm.yml ps

compose-down:
	@docker compose -f local/docker/compose.scillm.yml down -v

compose-logs:
	@docker compose -f local/docker/compose.scillm.yml logs -f --tail=200

compose-ps:
	@docker compose -f local/docker/compose.scillm.yml ps
smoke-budget-cost:
	@echo "Running budget+cost smoke via proxy"
	@PROXY_BASE=$${PROXY_BASE:-http://127.0.0.1:4010} \
	PROM_BASE=$${PROM_BASE:-http://127.0.0.1:9090} \
	PROXY_KEY=$${LITELLM_MASTER_KEY:-sk-dev-proxy-123} \
	SMOKE_MODEL=$${SMOKE_MODEL:-text-auto} \
	uv run -- python scripts/smoke_budget_cost.py
