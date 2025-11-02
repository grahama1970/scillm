#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
import threading
import time
from wsgiref.simple_server import make_server


def app(environ, start_response):
    if environ.get('PATH_INFO') != '/metrics':
        start_response('404 Not Found', [('Content-Type', 'text/plain')])
        return [b'not found']
    now = int(time.time())
    # Minimal set of SciLLM metrics to light up dashboards
    cost = 0.0123 + random.random() * 0.001
    body = f"""
# HELP sc_calls_total SciLLM calls
# TYPE sc_calls_total counter
sc_calls_total{{feature="text",result="ok",env="dev"}} 7
sc_calls_total{{feature="text",result="429",env="dev"}} 0

# HELP sc_request_seconds SciLLM request duration seconds
# TYPE sc_request_seconds histogram
sc_request_seconds_bucket{{feature="text",env="dev",le="0.1"}} 1
sc_request_seconds_bucket{{feature="text",env="dev",le="0.2"}} 3
sc_request_seconds_bucket{{feature="text",env="dev",le="0.5"}} 5
sc_request_seconds_bucket{{feature="text",env="dev",le="1"}} 6
sc_request_seconds_bucket{{feature="text",env="dev",le="+Inf"}} 7
sc_request_seconds_count{{feature="text",env="dev"}} 7
sc_request_seconds_sum{{feature="text",env="dev"}} 2.1

# HELP sc_budget_limit Budget limit (calls per window)
# TYPE sc_budget_limit gauge
sc_budget_limit{{feature="text",env="dev"}} 5000
# HELP sc_budget_remaining Budget remaining (calls)
# TYPE sc_budget_remaining gauge
sc_budget_remaining{{feature="text",env="dev"}} 4321
# HELP sc_reset_timestamp_seconds Next budget reset epoch seconds
# TYPE sc_reset_timestamp_seconds gauge
sc_reset_timestamp_seconds{{feature="text",env="dev"}} {now + 3600}

# HELP sc_cost_usd_total Accumulated request cost in USD
# TYPE sc_cost_usd_total counter
sc_cost_usd_total{{feature="text",vendor="chutes",model="moonshotai/Kimi-K2-Instruct-0905",env="dev"}} {cost:.6f}
""".strip().encode('utf-8')
    start_response('200 OK', [('Content-Type', 'text/plain; version=0.0.4')])
    return [body]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=9400)
    args = ap.parse_args()
    httpd = make_server('0.0.0.0', args.port, app)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

