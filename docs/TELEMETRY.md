# SciLLM Telemetry (Prometheus + Grafana)

This library does not bundle Grafana. Instead, it exposes standard Prometheus/OpenMetrics so your existing observability stack can scrape metrics and visualize them using the provided reference dashboard.

## Quick start (local)

1) Enable metrics in your process:
```
export SCILLM_METRICS=prom
export SCILLM_METRICS_ADDR=0.0.0.0
export SCILLM_METRICS_PORT=9464
```
SciLLM will start an in-process /metrics server at http://0.0.0.0:9464/metrics when first used.

2) Import and use helpers (no code changes required if you already call `scillm.extras.chutes_simple`). Metrics are recorded automatically on success and when tenacious retries occur.

3) Grafana
- Import `dashboards/grafana/scillm_prometheus_grafana.json` into your Grafana instance.
- Ensure your Prometheus is scraping the process (via static target or service discovery).

## Environment

- `SCILLM_METRICS=prom|prometheus`  Enable Prometheus /metrics endpoint.
- `SCILLM_METRICS_ADDR`             Bind address (default: 0.0.0.0)
- `SCILLM_METRICS_PORT`             Port (default: 9464)

## Metric names (low cardinality)

- `scillm_requests_total{route="direct|router", result="ok|error", retried="0|1", model_tier="text|vlm|tools"}`
- `scillm_request_latency_seconds_bucket{route, model_tier}`  (with *_count and *_sum)
- `scillm_retries_total{reason="429_capacity|timeout|5xx|other"}`
- `scillm_router_fallbacks_total{reason}`
- `scillm_requests_in_flight{route}`

Notes
- No secrets in metrics or labels. Do not add model IDs or API keys to labels.
- Tenacious mode records retry reasons and marks final request as `retried="1"` when applicable.
- If `prometheus-client` is not installed, metrics calls are no-ops (no failures, just disabled).

## Alerts (examples)

- High 429/503 rate: `sum(rate(scillm_retries_total{reason="429_capacity"}[15m])) > X`
- Latency SLO: `histogram_quantile(0.95, sum(rate(scillm_request_latency_seconds_bucket[5m])) by (le)) > Y`
- Fallbacks spike: `sum(rate(scillm_router_fallbacks_total[5m])) > Z`

