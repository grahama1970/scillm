# Project Knowledge: scillm

**Last updated:** 2026-04-14 15:10 by agent
**Status:** Active development

## Current Understanding

- scillm is a single-tenant LLM proxy at localhost:4001
- Direct provider routing via openai SDK (Bifrost removed 2026-04-13)
- All logging via ArangoDB (`llm_call_log` collection), NOT Redis
- Redis is ONLY for optional caching
- Silent batch failures are forbidden — must log raw responses for debugging

### CWE Batch Model Fallback Chain (Chutes only)

All models verified for 100% grounding accuracy on QRA task:

| # | Model Alias | Chutes Model ID | Notes |
|---|-------------|-----------------|-------|
| 1 | `text` | DeepSeek-* (dynamic) | chutes_router selects least saturated 671B variant |
| 2 | `deepseek-ai/DeepSeek-V3.1-TEE` | DeepSeek-V3.1-TEE | Fallback DeepSeek |
| 3 | `deepseek-ai/DeepSeek-R1-0528-TEE` | DeepSeek-R1-0528-TEE | Fallback DeepSeek |
| 4 | `text-kimi` | moonshotai/Kimi-K2.5-TEE | ~81s latency |
| 5 | `text-qwen3` | Qwen/Qwen3-235B-A22B-Thinking-2507 | Faster Qwen3 |
| 6 | `text-qwen3-large` | Qwen/Qwen3.5-397B-A17B-TEE | Slowest, last resort |

**NOT in batch chain:** OAuth providers (Codex, Claude), external DeepSeek API, Gemini

## Recent Decisions

| Date | Decision | Why |
|------|----------|-----|
| 2026-04-14 | CWE batch uses 6 Chutes models only | No OAuth (ban risk), no external DeepSeek (paid), no Gemini. All models verified 100% QRA grounding. Qwen3.5-397B is slowest last resort. |
| 2026-04-13 | Remove Bifrost gateway | Was never enabled (BIFROST_ENABLED=false). Direct openai SDK routing is simpler. ArangoDB llm_call_log provides all monitoring data. |
| 2026-04-13 | Build scillm batch dashboard in ux-lab | React components using EmbryStyle/NVIS tokens, queries ArangoDB for batch progress, per-skill usage, error rates |
| 2026-04-13 | Add caller_info fallback when x-caller-skill missing | Logs user_agent and other headers when skill header absent — helps identify unknown callers |
| 2026-04-13 | Fixed deprecated model refs in 4 skills | scillm/prove.py, ingest-audiobook, review-music, ingest-movie were using deprecated deepseek-ai/DeepSeek-V3 |
| 2026-04-13 | Automatic batch resume via scillm_metadata | Skills pass batch_id + item_id; scillm auto-skips completed items on retry |
| 2026-04-13 | Skill identification via x-caller-skill header | Per-skill usage tracking and error correlation in llm_call_log |
| 2026-04-13 | Add raw response logging to arango_log.py | Batch failures (0 stored) were impossible to debug without seeing LLM responses |
| 2026-04-13 | Clean up legacy Redis logs (75K entries) | Duplicate logging — all logging now via ArangoDB only |
| 2026-04-13 | Document misuse patterns in SKILL.md | Schema mismatches, silent failures, Redis logging are now documented anti-patterns |
| 2026-04-10 | Bifrost P1+P2 architecture | Go gateway for performance + Python for API translation |
| 2026-04-05 | Use fallbacks instead of priority field | litellm ignores model_info.priority — was silently broken |

## Open Questions

- [x] Why did batch store 0/1075 QRAs? → Schema mismatch (`reason` vs `abstain_reason`)
- [ ] Why wasn't caching preserving failed batch responses?

## Key Files

| File | Purpose |
|------|---------|
| `chutes/middleware/arango_log.py` | Logs every LLM call to ArangoDB with request/response content |
| `chutes/middleware/batch_resume.py` | Checks ArangoDB for completed work items (automatic batch resume) |
| `chutes/middleware/json_guard.py` | JSON validation and repair |
| `chutes/middleware/concurrency_guard.py` | Provider-aware semaphore (chutes=4, ollama=1) |
| `local/proxy_server_config.yaml` | Single source of truth for models/providers |
| `deploy/docker/compose.scillm.core.yml` | Production compose (scillm + utls-proxy) |
| `deploy/docker/Dockerfile.scillm` | Single-stage Python image (Bifrost removed) |
| `~/.pi/skills/scillm/SKILL.md` | Skill documentation with misuse patterns |
| `.archive/bifrost/` | Archived Bifrost code (removed 2026-04-13) |

## Misuse Patterns (Forbidden)

| Pattern | Why | Fix |
|---------|-----|-----|
| Silent batch failures | "0 stored" with no explanation wastes hours | Log first failure with expected vs actual schema |
| Schema mismatch | Checking wrong field names (e.g., `reason` vs `abstain_reason`) | Log raw `response_content` to `llm_call_log` |
| Redis for logging | Duplicate logging, wrong tool | Use ArangoDB via `arango_log.py` only |
| max_tokens | Causes truncated output | Never set it — auto-stripped |
| Fire-all-at-once batching | >4 requests causes queue timeout | Use CHUNK_SIZE=4 loop |
| Deprecated model names | `deepseek-ai/DeepSeek-V3` triggers abuse guard | Use aliases (`text`, `vlm`) not direct model names |
| Missing x-caller-skill | Can't debug which skill caused errors | Add header; fallback logs user_agent only |

## Infrastructure State

- **scillm proxy:** localhost:4001 (Docker, network_mode: host, direct provider routing)
- **utls-proxy:** localhost:8444 (TLS fingerprint for Codex)
- **ArangoDB logging:** llm_call_log collection via memory service :8601
- **Redis:** NOT used by scillm (embry-redis is for PCP system metrics only, see embry-os/docs/REDIS_PCP_USAGE.md)
- **Dashboard:** React components in ux-lab (planned), queries ArangoDB for batch monitoring
