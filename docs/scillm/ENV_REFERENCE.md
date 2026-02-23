# SciLLM Environment Variable Reference

Central index of key environment variables. Prefer `SCILLM_` prefix; legacy `LITELLM_` aliases remain for backward compatibility.

## Enable Flags
| Variable | Purpose | Alias |
|----------|---------|-------|
| `SCILLM_ENABLE_CODEX_AGENT` | Enable codex‑agent provider | `LITELLM_ENABLE_CODEX_AGENT` |
| `SCILLM_ENABLE_CODEWORLD` | Enable CodeWorld provider | `LITELLM_ENABLE_CODEWORLD` |
| `SCILLM_ENABLE_LEAN4` | Enable Lean4 bridge (Certainly) | `LITELLM_ENABLE_LEAN4`, `SCILLM_ENABLE_CERTAINLY` |
| `SCILLM_ENABLE_MINI_AGENT` | Enable mini‑agent integration | `LITELLM_ENABLE_MINI_AGENT` |

## Bases / Endpoints
| Variable | Notes |
|----------|-------|
| `CODEX_AGENT_API_BASE` | No `/v1` suffix (provider appends) |
| `CODEWORLD_BASE` | CodeWorld bridge base |
| `LEAN4_BRIDGE_BASE` / `CERTAINLY_BRIDGE_BASE` | Lean4/Certainly bridge base |

## Retry & Logging
| Variable | Effect |
|----------|--------|
| `SCILLM_RETRY_META` | Include retry metadata under `additional_kwargs.router.retries` |
| `SCILLM_LOG_JSON` | Structured JSON log lines |
| `SCILLM_RETRY_LOG_EVERY` | Emit every N retry attempts |

## CodeWorld MCTS / Autogen
| Variable | Default | Description |
|----------|---------|-------------|
| `CODEWORLD_MCTS_AUTO_N` | 3 | Number of autogen variants |
| `CODEWORLD_MCTS_AUTO_TEMPERATURE` | 0 | Temperature for variant generation |
| `CODEWORLD_MCTS_AUTO_MAX_TOKENS` | 2000 | Max tokens for autogen model |
| `CODEWORLD_MCTS_AUTO_MODEL` | provider default | Override generation model |
| `CODEWORLD_ONEPOST_TIMEOUT_S` | 60 | HTTP client timeout for one‑POST flow |

## Determinism / Reproducibility
| Variable | Description |
|----------|-------------|
| `SCILLM_DETERMINISTIC_SEED` | Seed applied to deterministic features (e.g., MCTS) |
| `RUN_ID` | Optional namespace for caching / manifests |

## Mini‑Agent
| Variable | Description |
|----------|-------------|
| `MINI_AGENT_STORE_TRACES` | If `1`, append JSONL traces |
| `MINI_AGENT_STORE_PATH` | Trace file path |

## Readiness & Warmups
| Variable | Description |
|----------|-------------|
| `STRICT_WARMUPS` | Enforce warm-up completion |
| `READINESS_LIVE` | Enable live readiness gate |
| `STRICT_READY` | Fail on any readiness gap |
| `READINESS_EXPECT` | Comma list of expected providers (e.g. `codeworld,certainly`) |

## Auth Placeholders
| Variable | Notes |
|----------|-------|
| `OPENAI_API_KEY` | Passed through to upstream compat surface |
| `CODEX_AGENT_API_KEY` | Used when echo mode disabled |
| (Other provider keys) | Standard LiteLLM semantics |

## Cache (Optional)
| Variable | Description |
|----------|-------------|
| `REDIS_HOST`, `REDIS_PORT` | Cache backend host/port; fallback to in‑memory if unset |

## Proxy & Paved Path
| Variable | Required | Default | Description |
|---|---|---|---|
| `SCILLM_PROXY_KEY` | No | `sk-dev-proxy-123` | API key for authenticating with the LiteLLM proxy (must match `LITELLM_MASTER_KEY`) |
| `SCILLM_API_BASE` | No | `http://localhost:4010` | Base URL for the LiteLLM proxy |
| `SCILLM_MODEL` | No | `text` | Default model name for text completions via proxy |
| `SCILLM_VISION_MODEL` | No | `vlm` | Default model name for vision completions via proxy |
| `CHUTES_TEXT_MODEL` | Yes (for Chutes) | — | Chutes vendor model ID for text (e.g. `deepseek-ai/DeepSeek-R1-0528`) |
| `CHUTES_VLM_MODEL` | Yes (for VLM) | — | Chutes vendor model ID for vision (e.g. `Qwen/Qwen2.5-VL-72B-Instruct`) |
| `LITELLM_MASTER_KEY` | No | `sk-dev-proxy-123` | Master key for the LiteLLM proxy server |
| `GEMINI_API_KEY` | No | — | Google Gemini API key (2nd priority text fallback) |
| `DEEPSEEK_API` | No | — | DeepSeek API key (3rd priority text fallback) |

## Security Notes
- Disable codex sidecar echo (`CODEX_SIDECAR_ECHO=1`) before using real credentials.
- Treat autogen code/proofs as untrusted until verified.

---
See also: [FEATURES.md](FEATURES.md), [QUICKSTART.md](QUICKSTART.md), [docs/guide/RATE_LIMIT_RETRIES.md](../guide/RATE_LIMIT_RETRIES.md).
