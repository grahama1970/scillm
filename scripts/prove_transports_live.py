"""Live proof for issues #27/#28: profiles readiness readback + normalized transports.

Runs against a live scillm proxy (default http://127.0.0.1:4102) with real
credentials. Emits a JSON receipt to stdout. Exit 0 only if every proof passed.
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("SCILLM_PROOF_BASE", "http://127.0.0.1:4102")
KEY = os.environ.get("SCILLM_MASTER_KEY") or os.environ.get("LITELLM_MASTER_KEY") or ""
HDRS = {"Authorization": f"Bearer {KEY}", "X-Caller-Skill": "ticket-27-28-proof"}

receipt: dict = {"base": BASE, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "proofs": {}}
ok_all = True


def record(name: str, ok: bool, detail):
    global ok_all
    ok_all = ok_all and ok
    receipt["proofs"][name] = {"ok": ok, "detail": detail}


def wait_result(client: httpx.Client, tid: str, timeout: float = 180.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = client.get(f"{BASE}/v1/scillm/transports/{tid}/result", params={"wait_sec": 10}, headers=HDRS)
        if r.status_code == 200:
            return r.json()
        time.sleep(1)
    raise TimeoutError(f"no result for {tid}")


def main() -> int:
    if "--live" not in sys.argv:
        print("this proof performs real provider calls; re-run with --live", file=sys.stderr)
        return 2
    client = httpx.Client(timeout=240)

    # -- #27: live readiness readback (OAuth model-turn profile + one more) --
    for prof in ("claude-model-turn", "gemini-vlm"):
        r = client.get(f"{BASE}/v1/scillm/profiles/readiness", params={"profile": prof, "live": "true"}, headers=HDRS)
        rec = r.json()["readiness"][0] if r.status_code == 200 else {"http": r.status_code, "body": r.text[:300]}
        record(
            f"readiness_live_{prof}",
            r.status_code == 200 and rec.get("state") == "transport_live_ready" and bool(rec.get("evidence", {}).get("live_probe", {}).get("upstream_id")),
            rec,
        )

    # negative: unknown profile fails closed
    r = client.get(f"{BASE}/v1/scillm/profiles/readiness", params={"profile": "ghost"}, headers=HDRS)
    record("readiness_unknown_profile_404", r.status_code == 404, {"http": r.status_code})

    # negative: unknown capability fails closed
    r = client.get(f"{BASE}/v1/scillm/profiles/capabilities", params={"require": "telepathy"}, headers=HDRS)
    record("capabilities_unknown_cap_422", r.status_code == 422, {"http": r.status_code})

    # -- #28: live OAuth one-turn model call through the normalized API --
    req = {
        "schema": "scillm.transport_request.v1",
        "profile": "claude-model-turn",
        "correlation": {"tau_run_id": "proof-run", "node_id": "ticket-28", "attempt": 1, "goal_hash": "proof"},
        "messages": [{"role": "user", "content": "Reply with exactly: transport proof ok"}],
    }
    r = client.post(f"{BASE}/v1/scillm/transports", json=req, headers=HDRS)
    handle = r.json()
    result = wait_result(client, handle["transport_id"]) if r.status_code == 201 else {}
    events = client.get(f"{BASE}/v1/scillm/transports/{handle.get('transport_id','x')}/events", headers=HDRS).json() if r.status_code == 201 else {}
    record(
        "live_oauth_one_turn",
        r.status_code == 201
        and result.get("ok") is True
        and result.get("state") == "turn_completed"
        and bool(result.get("upstream", {}).get("id"))
        and result.get("correlation", {}).get("goal_hash") == "proof"
        and any(e["type"] == "assistant_message" for e in events.get("events", [])),
        {"handle": handle, "result": {k: result.get(k) for k in ("ok", "state", "upstream", "usage", "model")}, "event_types": [e["type"] for e in events.get("events", [])]},
    )

    # -- #28: live multi-turn tool-call canary; harness (this script) is the tool executor --
    tool_req = {
        "schema": "scillm.transport_request.v1",
        "profile": "claude-model-turn",
        "correlation": {"tau_run_id": "proof-run", "node_id": "ticket-28-tools", "attempt": 1},
        "messages": [{"role": "user", "content": "Use the get_answer tool to fetch the answer, then state it as: answer=<value>"}],
        "tools": [{
            "type": "function",
            "function": {
                "name": "get_answer",
                "description": "Returns the canonical answer string",
                "parameters": {"type": "object", "properties": {}},
            },
        }],
    }
    r = client.post(f"{BASE}/v1/scillm/transports", json=tool_req, headers=HDRS)
    canary = {"create_http": r.status_code}
    tool_ok = False
    if r.status_code == 201:
        tid = r.json()["transport_id"]
        res1 = wait_result(client, tid)
        canary["turn0_state"] = res1.get("state")
        if res1.get("state") == "awaiting_tool_result":
            ev = client.get(f"{BASE}/v1/scillm/transports/{tid}/events", headers=HDRS).json()["events"]
            tool_calls = next(e for e in ev if e["type"] == "tool_call_request")["data"]["tool_calls"]
            canary["tool_call"] = tool_calls[0]["function"]["name"]
            r2 = client.post(
                f"{BASE}/v1/scillm/transports/{tid}/turns",
                json={"tool_results": [{"tool_call_id": tool_calls[0]["id"], "content": "PROOF-42"}]},
                headers=HDRS,
            )
            canary["turn_http"] = r2.status_code
            res2 = wait_result(client, tid)
            canary["turn1_state"] = res2.get("state")
            canary["turn1_upstream"] = res2.get("upstream")
            final = client.get(f"{BASE}/v1/scillm/transports/{tid}/events", headers=HDRS).json()["events"]
            texts = [e["data"].get("content", "") for e in final if e["type"] == "assistant_message"]
            canary["final_text"] = texts[-1] if texts else ""
            tool_ok = res2.get("state") == "turn_completed" and "PROOF-42" in canary["final_text"]
    record("live_tool_call_canary", tool_ok, canary)

    # -- #28: positive cancellation proof --
    slow_req = {
        "schema": "scillm.transport_request.v1",
        "profile": "claude-model-turn",
        "correlation": {"tau_run_id": "proof-run", "node_id": "ticket-28-cancel", "attempt": 1},
        "messages": [{"role": "user", "content": "Write a 2000 word essay about transport layers."}],
    }
    r = client.post(f"{BASE}/v1/scillm/transports", json=slow_req, headers=HDRS)
    cancel_detail = {"create_http": r.status_code}
    cancel_ok = False
    if r.status_code == 201:
        tid = r.json()["transport_id"]
        time.sleep(1.0)
        rc = client.post(f"{BASE}/v1/scillm/transports/{tid}/cancel", headers=HDRS)
        cancel_detail["cancel_http"] = rc.status_code
        cancel_detail["state"] = rc.json().get("state") if rc.status_code == 200 else rc.json()
        cancel_ok = rc.status_code == 200 and rc.json().get("state") == "cancelled"
    record("live_cancel_in_flight", cancel_ok, cancel_detail)

    # negative: next turn on cancelled transport → typed unsupported
    if cancel_ok:
        rn = client.post(f"{BASE}/v1/scillm/transports/{tid}/turns", json={"messages": [{"role": "user", "content": "resume"}]}, headers=HDRS)
        record("cancelled_resume_typed_unsupported", rn.status_code == 409 and rn.json()["error"]["type"] == "unsupported", rn.json())

    # negative: unknown transport profile on create
    r = client.post(f"{BASE}/v1/scillm/transports", json={**req, "profile": "ghost"}, headers=HDRS)
    record("transport_unknown_profile_404", r.status_code == 404, {"http": r.status_code})

    # negative: required capability unsatisfied (local-text lacks tool_calling)
    r = client.post(
        f"{BASE}/v1/scillm/transports",
        json={**req, "profile": "local-text", "required_capabilities": ["tool_calling"]},
        headers=HDRS,
    )
    record("transport_missing_capability_422", r.status_code == 422, {"http": r.status_code, "body": r.json()})

    # opaque compat is visibly distinct and cannot run as a native turn
    r = client.post(f"{BASE}/v1/scillm/transports", json={**req, "profile": "opencode-serve-compat"}, headers=HDRS)
    body = r.json()
    record(
        "opaque_compat_fork_required",
        r.status_code == 409 and body.get("error", {}).get("type") == "fork_required",
        body,
    )

    receipt["ok"] = ok_all
    print(json.dumps(receipt, indent=2))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
