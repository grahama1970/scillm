# scillm Makefile — proxy operations, smokes, and tooling
# Production proxy: docker compose up -d --build

# ── Proxy ─────────────────────────────────────────────────────────────
.PHONY: proxy-up proxy-down proxy-logs proxy-rebuild

proxy-up:
	docker compose -p scillm up -d
	@echo "scillm proxy + Redis on :4001"

proxy-down:
	docker compose -p scillm down

proxy-logs:
	docker compose -p scillm logs -f --tail=200

proxy-rebuild:
	docker compose -p scillm up -d --build
	@echo "scillm proxy rebuilt and running on :4001"

# ── Chutes Endpoint Toggle ────────────────────────────────────────────
.PHONY: chutes-research chutes-standard chutes-endpoint chutes-doctor

chutes-research:
	@sed -i 's|^CHUTES_RESEARCH=.*|CHUTES_RESEARCH=1|' .env
	@sed -i 's|^CHUTES_API_BASE=.*|CHUTES_API_BASE="https://research-data-opt-in.chutes.ai/v1"|' .env
	@echo "Chutes → research endpoint (25% off). Rebuild proxy to apply."

chutes-standard:
	@sed -i 's|^CHUTES_RESEARCH=.*|CHUTES_RESEARCH=0|' .env
	@sed -i 's|^CHUTES_API_BASE=.*|CHUTES_API_BASE="https://llm.chutes.ai/v1"|' .env
	@echo "Chutes → standard endpoint (full price). Rebuild proxy to apply."

chutes-endpoint:
	@grep '^CHUTES_API_BASE=' .env | head -1
	@grep '^CHUTES_RESEARCH=' .env | head -1

chutes-doctor:
	@if [ -z "$$CHUTES_API_BASE" ] || [ -z "$$CHUTES_API_KEY" ] || [ -z "$$CHUTES_TEXT_MODEL" ]; then \
	  echo "skip: set CHUTES_API_BASE, CHUTES_API_KEY, CHUTES_TEXT_MODEL"; \
	  exit 0; \
	fi; \
	python scripts/chutes_doctor.py

# ── Smoke Tests ───────────────────────────────────────────────────────
.PHONY: smoke-ollama smoke-chat-headers smoke-budget smoke-demo-pricing smokes-cli-fast smokes

smoke-ollama:
	@python3 scripts/smoke_ollama.py

smoke-chat-headers:
	@SMOKE_MODEL=$${SMOKE_MODEL:-local-text} python3 scripts/smoke_chat_headers.py

smoke-budget:
	@python3 scripts/smoke_budget_endpoint.py

smoke-demo-pricing:
	@SMOKE_MODEL=$${SMOKE_MODEL:-local-text} python3 scripts/smoke_demo_pricing.py

# Quality gate (proxy-only, no Ollama preflight)
smokes-cli-fast:
	@$(MAKE) --no-print-directory smoke-chat-headers
	@$(MAKE) --no-print-directory smoke-budget
	@$(MAKE) --no-print-directory smoke-demo-pricing

# Full suite including Ollama preflight
smokes:
	@$(MAKE) --no-print-directory smoke-ollama
	@$(MAKE) --no-print-directory smoke-chat-headers
	@$(MAKE) --no-print-directory smoke-budget
	@$(MAKE) --no-print-directory smoke-demo-pricing

# E2E tests (pytest, hits live proxy)
.PHONY: test-e2e test-adversarial test-grounding
test-e2e:
	python -m pytest tests/test_proxy_e2e.py -v

# Adversarial tests (auth, streaming, error propagation, edge cases)
test-adversarial:
	python -m pytest tests/test_proxy_adversarial.py -v

# Grounding unit tests
test-grounding:
	python -m pytest tests/test_grounding.py -v

# ── Guards ────────────────────────────────────────────────────────────
.PHONY: grep-guard check-root-layout check-no-secrets-logs

grep-guard:
	@bash scripts/grep_guard_scillm.sh /home/graham/workspace/experiments/extractor /home/graham/workspace/experiments/sparta /home/graham/workspace/experiments/memory

check-root-layout:
	python3 scripts/check_root_layout.py

check-no-secrets-logs:
	@! rg -n "Authorization:\s*Bearer\s+\w" -S . || (echo "Found Authorization token in repo logs/text" && exit 1)
	@! rg -n "x-api-key:\s*\w" -S . || (echo "Found x-api-key in repo logs/text" && exit 1)

# ── Budget Gateway Lite ───────────────────────────────────────────────
.PHONY: budget-lite-build budget-lite-run budget-lite-smoke

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
