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

# Front door: models + JSON + VLM sanity (uses Bearer + /v1). Fails if any missing.
.PHONY: chutes-front-door
chutes-front-door:
	@if [ -z "$$CHUTES_API_BASE" ] || [ -z "$$CHUTES_API_KEY" ]; then \
	  echo "error: set CHUTES_API_BASE and CHUTES_API_KEY"; exit 2; \
	fi; \
	PYTHONPATH=$$(pwd)/src SCILLM_AUTOSCALE=1 python scripts/tools/scillm_quick_doctor.py

.PHONY: chutes-doctor-vlm
chutes-doctor-vlm:
	@if [ -z "$$CHUTES_API_BASE" ] || [ -z "$$CHUTES_API_KEY" ] || [ -z "$$CHUTES_VLM_MODEL" ]; then \
	  echo "error: set CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_VLM_MODEL"; exit 2; \
	fi; \
	PYTHONPATH=$$(pwd)/src python scripts/tools/scillm_multimodal_sanity.py --model "$$CHUTES_VLM_MODEL" --run-curl

# Local CI-style smoke (JSON report + exit code)
.PHONY: ci-core
ci-core:
	PYTHONPATH=$$(pwd)/src python scripts/ci/scillm_core_check.py

.PHONY: check-root-layout
check-root-layout:
	python3 scripts/check_root_layout.py
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

# Convenience: run the proxy with a demo pricing file to make sc_cost_usd_total move during demos
proxy-run-uv-demo-pricing:
	@if ! command -v uv >/dev/null 2>&1; then echo "uv not installed"; exit 2; fi
	@if [ ! -f local/proxy_server_config.yaml ]; then echo "missing local/proxy_server_config.yaml"; exit 2; fi
	@if [ ! -f examples/pricing/example.json ]; then echo "missing examples/pricing/example.json"; exit 2; fi
	LITELLM_MASTER_KEY=$${LITELLM_MASTER_KEY:-sk-dev-proxy-123} METRICS_ENV=$${METRICS_ENV:-dev} \
	CHUTES_PRICING_FILE=examples/pricing/example.json \
	uv run litellm --config local/proxy_server_config.yaml --host 0.0.0.0 --port $${PORT:-4010} --log_level warning

# Print how to enable demo pricing in your current shell (non-persistent)
demo-pricing:
	@echo "To enable demo pricing for this shell, run:"
	@echo "\n  export CHUTES_PRICING_FILE=examples/pricing/example.json\n"

# Run a one-shot smoke that asserts sc_cost_usd_total increases after one chat call
smoke-demo-pricing:
	@SMOKE_MODEL=$${SMOKE_MODEL:-local-text} python3 scripts/smoke_demo_pricing.py

# One JSON chat via proxy and assert budget headers present
smoke-chat-headers:
	@SMOKE_MODEL=$${SMOKE_MODEL:-local-text} python3 scripts/smoke_chat_headers.py

# GET /v1/budget and validate minimal contract
smoke-budget:
	@python3 scripts/smoke_budget_endpoint.py

# Run all local smokes that do not require external provider secrets.
smokes:
	@$(MAKE) --no-print-directory smoke-chat-headers
	@$(MAKE) --no-print-directory smoke-budget
	@$(MAKE) --no-print-directory smoke-demo-pricing

prom-run-docker:
	@printf '%s\n' \
	  'global:' \
	  '  scrape_interval: 15s' \
	  'scrape_configs:' \
	  '- job_name: litellm' \
	  '  static_configs:' \
	  "  - targets: ['host.docker.internal:$${PORT:-4010}']" > /tmp/prom.yml
	@docker rm -f scillm-prom >/dev/null 2>&1 || true
	docker run -d --name scillm-prom --add-host=host.docker.internal:host-gateway -p 9090:9090 -v /tmp/prom.yml:/etc/prometheus/prometheus.yml prom/prometheus:latest
	@echo "Prometheus listening on :9090"

prom-run-docker-lite:
	@printf '%s\n' \
	  'global:' \
	  '  scrape_interval: 15s' \
	  'scrape_configs:' \
	  '- job_name: budget-lite' \
	  '  static_configs:' \
	  "  - targets: ['host.docker.internal:$${PORT:-4011}']" > /tmp/prom_bl.yml
	@docker rm -f scillm-prom-bl >/dev/null 2>&1 || true
	docker run -d --name scillm-prom-bl --add-host=host.docker.internal:host-gateway -p 9091:9090 -v /tmp/prom_bl.yml:/etc/prometheus/prometheus.yml prom/prometheus:latest
	@echo "Prometheus (budget-lite) listening on :9091"

grafana-import-lite:
	@if [ -z "$$GRAFANA_URL" ] || [ -z "$$GRAFANA_TOKEN" ]; then \
	  echo "Usage: GRAFANA_URL=... GRAFANA_TOKEN=... make grafana-import-lite"; exit 2; fi
	python3 scripts/grafana_import_dashboards.py --dash dashboards/scillm_budget_lite_grafana.json

proxy-run-docker-single:
	@echo "Building image (if needed) and running stateless proxy container on :4010"
	@if [ -z "$$CHUTES_API_BASE" ] || [ -z "$$CHUTES_API_KEY" ] || [ -z "$$CHUTES_TEXT_MODEL" ]; then \
	  echo "Set CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_TEXT_MODEL in your shell or .env before running."; \
	  exit 2; \
	fi
	@docker build -t scillm/app:local -f deploy/docker/Dockerfile.scillm . >/dev/null
	@docker rm -f scillm-proxy >/dev/null 2>&1 || true
	@docker run -d --name scillm-proxy -p 4010:4010 \
	  -e LITELLM_MASTER_KEY=$${LITELLM_MASTER_KEY:-12345} \
	  -e METRICS_ENV=$${METRICS_ENV:-dev} \
	  -e SCILLM_BUDGET_METADATA=$${SCILLM_BUDGET_METADATA:-1} \
	  -e CHUTES_API_BASE="$$CHUTES_API_BASE" \
	  -e CHUTES_API_KEY="$$CHUTES_API_KEY" \
	  -e CHUTES_TEXT_MODEL="$$CHUTES_TEXT_MODEL" \
	  -v "$$(pwd)/local/proxy_server_config.yaml:/app/config.yaml:ro" \
	  scillm/app:local \
	  sh -lc "litellm --config /app/config.yaml --host 0.0.0.0 --port 4010" >/dev/null
	@echo "Proxy listening on :4010 (master key=$${LITELLM_MASTER_KEY:-12345})"

# Budget Gateway Lite (Chutes forwarder, stateless)
budget-lite-build:
	@docker build -t scillm/budget-lite:local -f local/budget_gateway_lite/Dockerfile .

budget-lite-run:
	@if [ -z "$$CHUTES_API_BASE" ] || [ -z "$$CHUTES_API_KEY" ] || [ -z "$$CHUTES_TEXT_MODEL" ]; then \
	  echo "Set CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_TEXT_MODEL"; exit 2; fi
	@docker rm -f scillm-budget-lite >/dev/null 2>&1 || true
	docker run -d --name scillm-budget-lite -p 4011:4011 \
	  -e CHUTES_API_BASE -e CHUTES_API_KEY -e CHUTES_TEXT_MODEL \
	  -e BUDGET_PLAN=$${BUDGET_PLAN:-pro} \
	  -e BUDGET_DAILY_LIMIT=$${BUDGET_DAILY_LIMIT:-5000} \
	  -e BUDGET_RESET_UTC_HOUR=$${BUDGET_RESET_UTC_HOUR:-0} \
	  -e METRICS_ENV=$${METRICS_ENV:-dev} \
	  scillm/budget-lite:local
	@echo "Budget Gateway Lite listening on :4011"

budget-lite-smoke:
	@printf '%s' '{"model":"'"$${CHUTES_TEXT_MODEL}"'","messages":[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],"response_format":{"type":"json_object"},"temperature":0,"max_tokens":32}' > /tmp/bl_payload.json
	@curl -s -D /tmp/bl_h -H 'Content-Type: application/json' --data @/tmp/bl_payload.json http://127.0.0.1:4011/v1/chat/completions > /tmp/bl_resp.json || true
	@echo "-- headers --"; rg -n "^x-(ratelimit|budget)" -i /tmp/bl_h || true
	@echo "-- budget meta --"; jq '.additional_kwargs.scillm.budget' /tmp/bl_resp.json || true

proxy-docker-smoke:
	@echo "Running smoke against proxy :4010 (model gpt-chutes)"
	@printf '%s' '{"model":"gpt-chutes","messages":[{"role":"user","content":"Return only {\"ok\":true} as JSON."}],"response_format":{"type":"json_object"},"temperature":0,"max_tokens":32}' > /tmp/payload.json
	@curl -s -D /tmp/h -H "Authorization: Bearer $${LITELLM_MASTER_KEY:-12345}" -H 'Content-Type: application/json' --data @/tmp/payload.json http://127.0.0.1:4010/v1/chat/completions > /tmp/r.json || true
	@echo "-- headers --"; rg -n "^x-(ratelimit|budget|estimated)" -i /tmp/h || true
	@echo "-- budget meta --"; jq '.additional_kwargs.scillm.budget' /tmp/r.json || true

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
grafana-audit:
	@echo "Running Grafana audit (smoke + panel queries)"
	@mkdir -p artifacts
	@echo "Refreshing Grafana provisioning (container restart)..."
	@docker restart scillm-grafana >/dev/null 2>&1 || true
	@sleep 3
	@echo "Triggering budget-lite smoke to generate metrics..."
	@$(MAKE) -s budget-lite-smoke || true
	@echo "Waiting one scrape interval..."; sleep 15
	@ts=$$(date +%Y%m%dT%H%M%S); \
	  out=artifacts/grafana_audit_$${ts}.json; \
	  echo "Writing report to $$out"; \
	  uv run -- python scripts/grafana_audit.py > $$out || true; \
	  echo "Summary:"; jq -r '{ok, env, window, base} | @json' $$out; \
	  echo "Per-dashboard panel statuses:"; jq -r '.reports[] | "\(.title): " + ((.panels // []) | map(select(.ok==false)) | if length==0 then "OK" else (map(.title+" ["+(.status|tostring)+"]") | join(", ")) end)' $$out
	@echo "Done. Inspect artifacts/ for the detailed JSON report."

grafana-audit-quick:
	@mkdir -p artifacts
	@ts=$$(date +%Y%m%dT%H%M%S); out=artifacts/grafana_audit_$${ts}.json; \
	  uv run -- python scripts/grafana_audit.py > $$out || true; \
	  echo "Summary:"; jq -r '{ok, env, window, base} | @json' $$out

grafana-screens:
	@echo "Capturing full-page screenshots for core dashboards"
	@mkdir -p artifacts
	@uv run -- python scripts/grafana_screenshot.py
	@echo "Saved screenshots under artifacts/."
