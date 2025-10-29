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

---

# End‑to‑End: How projects monitor SciLLM batches with Grafana

1) What SciLLM provides

- Enables a Prometheus/OpenMetrics endpoint when you set:
  - `SCILLM_METRICS=prom`
  - `SCILLM_METRICS_ADDR=0.0.0.0` (default)
  - `SCILLM_METRICS_PORT=9464` (default)
- Records low‑cardinality metrics for:
  - Requests (count, latency histogram, in‑flight)
  - Retries (reasons: 429/capacity, timeout, 5xx, other)
  - Router fallbacks
- Includes a reference Grafana dashboard JSON: `dashboards/grafana/scillm_prometheus_grafana.json`.

2) What your project does

- Run batch workers/jobs with metrics enabled.
- Point Prometheus (or Grafana Agent) at the workers so it can scrape `/metrics`.
- Add a Prometheus data source in Grafana and import the SciLLM dashboard JSON.
- Optional alerts on retry rate, latency p95, and fallback spikes.

3) Common deployment patterns

### A. Docker / docker‑compose

- Expose port 9464 in your batch container and set the `SCILLM_METRICS` env vars.
- Minimal `prometheus.yml` scraping example:

```yaml
global:
  scrape_interval: 15s

scrape_configs:
- job_name: scillm-batch
  static_configs:
  - targets:
    - worker1:9464
    - worker2:9464
```

### B. Kubernetes (recommended for long‑running batches)

- In your Pod/Deployment, set `SCILLM_METRICS=prom` and expose a `containerPort: 9464`.
- Add a Service and a ServiceMonitor (Prometheus Operator) or PodMonitor to scrape.

```yaml
apiVersion: v1
kind: Service
metadata:
  name: scillm-batch-metrics
  labels:
    app: scillm-batch
spec:
  selector:
    app: scillm-batch
  ports:
  - name: http-metrics
    port: 9464
    targetPort: 9464
---
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: scillm-batch
  labels:
    release: prometheus
spec:
  selector:
    matchLabels:
      app: scillm-batch
  endpoints:
  - port: http-metrics
    interval: 15s
```

### C. Grafana Agent (remote_write)

- Run Grafana Agent as a sidecar/DaemonSet; it scrapes `:9464/metrics` and remote_writes to central Prometheus/Grafana Cloud.
- Good for ephemeral/auto‑scaling fleets.

### D. Ephemeral jobs (short‑lived)

- Prefer scrape‑based collection with Agent/SD when jobs live minutes+.
- If jobs are very short, consider a Pushgateway only as a last resort (with strict TTL/cleanup), or emit metrics from a long‑running coordinator.

4) Viewing long/large batches in Grafana

- The included dashboard charts RPS, retry rate/reasons, latency p95 by route, fallbacks, and in‑flight.

PromQL examples:
- Retry rate (%):
  `100 * (sum(rate(scillm_requests_total{retried="1"}[5m])) / sum(rate(scillm_requests_total[5m])))`
- Latency p95 by route:
  `histogram_quantile(0.95, sum(rate(scillm_request_latency_seconds_bucket[5m])) by (le, route))`
- Capacity pressure:
  `sum(rate(scillm_retries_total{reason="429_capacity"}[15m]))`

Alert ideas:
- High 429/capacity: `sum(rate(scillm_retries_total{reason="429_capacity"}[15m])) > X`
- Latency SLO breach (p95): `histogram_quantile(0.95, sum(rate(scillm_request_latency_seconds_bucket[5m])) by (le)) > Y`
- Fallback spike: `sum(rate(scillm_router_fallbacks_total[5m])) > Z`

5) Correlating to specific runs (batches)

- Avoid high‑cardinality labels like run_id/model_id in metrics.
- Prefer logs/traces for per‑run drill‑down (Loki/Tempo/OTEL).
- Add a batch summary log line at start/end with run_id, counts, status.
- If your stack supports exemplars, enable them on latency histograms and link to traces.

6) Security & reliability

- Bind metrics to internal interfaces (e.g., `SCILLM_METRICS_ADDR=127.0.0.1`) and use NetworkPolicy/SGs.
- Keep scrape intervals modest (15–30s) for long batches.
- Ensure no secrets appear in metrics; SciLLM never labels with tokens.

7) Quick checklist

- Workers: `export SCILLM_METRICS=prom`, expose 9464.
- Observability: Prometheus/Agent scrapes workers; Grafana has a Prometheus data source and imports the provided dashboard.
- Alerts: add retry rate, latency p95, and fallback spikes.
