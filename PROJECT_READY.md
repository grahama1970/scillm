# Project Readiness

Policy: DEV; READINESS_EXPECT=(none)

Resolved endpoints:
- mini-agent: 127.0.0.1:8788
- codex-agent base: http://127.0.0.1:8788
- ollama: http://127.0.0.1:11434

## Results
- ✅ deterministic_local
- ✅ mini_agent_e2e_low
- ✅ codex_agent_router_shim
- ✅ deterministic_local
- ⏭ mini_agent_e2e_low
- ✅ codex_agent_router_shim
- ✅ mini_agent_api_live_minimal
- ⏭ mini_agent_lang_tools
- ✅ mini_agent_escalation_high
- ⏭ lean4_bridge_smoke
- ⏭ codeworld_bridge_smoke
- ⏭ lean4_health
- ⏭ lean4_health_strict
- ⏭ certainly_health
- ⏭ codeworld_health
- ⏭ bridges_fullstack_health
- ⏭ bridges_fullstack_health_strict
- ⏭ coq_bridge_smoke
- ✅ chutes_warmup
- ✅ runpod_warmup
- ✅ chutes_warmup_strict
- ✅ runpod_warmup_strict
- ✅ warmups_strict_all
- ⏭ grounded_compare_smoke
- ✅ grounded_compare_strict
- ⏭ scenarios_live
- ❌ docker_smokes
- ⏭ codex_agent_live

Artifacts:
- local/artifacts/mvp/mvp_report.json
