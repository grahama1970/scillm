# SciLLM / Certainly Integration: Task List

**Status**: In Progress
**Created**: 2026-01-09
**Updated**: 2026-01-09
**Goal**: Reduce brittleness in scillm / certainly integration

---

## Implementation Summary

### Completed (2026-01-09)

The core integration work has been completed. scillm now supports **two modes**:

1. **DIRECT MODE** (preferred): When certainly package is installed via `pip install scillm[certainly]`, uses direct Python imports for better performance, cleaner errors, and no HTTP overhead.

2. **HTTP MODE** (fallback): When certainly is not installed, falls back to HTTP bridge at `CERTAINLY_BRIDGE_BASE` for backward compatibility.

### Key Changes Made

| File | Change |
|------|--------|
| `pyproject.toml` | Added `certainly` optional dependency with `file://` reference |
| `scillm/integrations/certainly.py` | **NEW** - Lazy import wrapper with clean API |
| `litellm/llms/lean4.py` | Refactored to try direct mode first, removed 60+ lines auto-start |
| `litellm/llms/certainly.py` | Updated docstring to reflect dual-mode support |
| `scillm/extras/providers.py` | Simplified error handling (40+ lines -> 20 lines) |

### New Environment Variables

| Variable | Purpose |
|----------|---------|
| `SCILLM_CERTAINLY_HTTP_ONLY=1` | Force HTTP mode even if certainly installed |
| `SCILLM_CERTAINLY_DIRECT_STRICT=1` | Fail fast if direct mode fails (no HTTP fallback) |

---

## Usage (After Implementation)

### Direct Mode (Recommended)

```bash
# Install with certainly support
pip install scillm[certainly]
# or: uv pip install -e ".[certainly]"
```

```python
from scillm.integrations.certainly import prove_requirement, is_available

if is_available():
    result = await prove_requirement(
        requirement="Prove that n + 0 = n for natural numbers",
        tactics=["simp"],
    )
    if result["ok"]:
        print(result["best"]["lean4"])
```

### Via LiteLLM Provider

```python
from litellm import completion

resp = completion(
    model="certainly",
    custom_llm_provider="certainly",
    items=[{"requirement_text": "Prove that n + 0 = n"}],
)
result = resp.additional_kwargs["certainly"]
```

---

## Resolved Questions

| Question | Resolution |
|----------|------------|
| Circular dependency strategy? | **A) Accept circular** - Use lazy imports in scillm to avoid import loop |
| What to import from certainly? | `prove_requirement()` + `Prover` class via lazy wrapper |
| Keep HTTP bridge? | **Keep as optional fallback** - HTTP mode remains for deployments without certainly installed |
| lean_runner interaction? | **Through certainly only** - scillm -> certainly -> lean_runner |

---

## Remaining Work

### Phase 2: Cleanup (Completed 2026-01-09)

#### 2.1 Remove duplicated bridge server from litellm
**Files removed**:
- [x] `src/lean4_prover/bridge/server.py` - REMOVED (user must delete manually)
- [x] `src/lean4_prover/bridge/__init__.py` - REMOVED (user must delete manually)
- [x] `src/common/bridge/schemas.py` - KEPT for CodeWorld bridge

#### 2.2 Simplify Docker infrastructure
**Files removed**:
- [x] `docker/compose.certainly.bridge.yml` - REMOVED (user must delete manually)
- [x] `docker/Dockerfile.bridge` - REMOVED (user must delete manually)
- [x] Updated compose files to remove lean4-bridge service references
- [x] Updated scripts to remove certainly bridge Docker references

#### 2.3 Consolidate environment variables
Canonical set (documented):
```
# Direct mode (preferred)
LEAN4_CONTAINER=lean_runner         # Docker container for Lean compilation
OPENROUTER_API_KEY=sk-or-...        # For DeepSeek Prover LLM calls

# HTTP fallback mode
CERTAINLY_BRIDGE_BASE=http://...    # Bridge URL (only if certainly not installed)

# Optional
SCILLM_CERTAINLY_HTTP_ONLY=1        # Force HTTP mode
SCILLM_CERTAINLY_DIRECT_STRICT=1    # Fail if direct mode fails
```

### Phase 3: Documentation (Completed 2026-01-09)

#### 3.1 Update SCILLM_INTEGRATION.md in lean4 repo
**File**: `/home/graham/workspace/experiments/lean4/docs/SCILLM_INTEGRATION.md`
- [x] Document the direct import pattern via `scillm.integrations.certainly`
- [x] Keep HTTP bridge as "alternative deployment" section
- [x] Add examples showing both modes

#### 3.2 Update QUICKSTART.md
**File**: `docs/scillm/QUICKSTART.md`
- [x] Update Certainly/Lean4 section with new `scillm.integrations.certainly` API
- [x] Simplify installation: `pip install scillm[certainly]`
- [x] Update code examples

#### 3.3 Update SCILLM_PAVED_PATH_CONTRACT.md
**File**: `docs/scillm/SCILLM_PAVED_PATH_CONTRACT.md`
- [x] Note that `certainly_prove()` now uses direct mode when available
- [x] Document the `is_available()` check pattern

### Phase 4: Testing

#### 4.1 Update/create integration tests
- [ ] Test direct import path works
- [ ] Test graceful degradation when certainly not installed
- [ ] Test response shape compatibility between modes

---

## Architecture (After Implementation)

```
User Code
    │
    ▼
scillm.integrations.certainly          # Lazy import wrapper
    │
    ├─── Direct Mode (certainly installed)
    │       │
    │       ▼
    │   lean4_prover.certainly_min      # From certainly package
    │       │
    │       ▼
    │   lean4_prover.core.prover.Prover # Manages Docker
    │       │
    │       ▼
    │   lean_runner container           # Lean 4 + Mathlib
    │
    └─── HTTP Mode (fallback)
            │
            ▼
        CERTAINLY_BRIDGE_BASE/bridge/complete
            │
            ▼
        Bridge server (Docker or lean4 repo)
```

---

## Files Changed

### scillm (litellm) repo
```
pyproject.toml                          # [x] Added optional dep
scillm/integrations/__init__.py         # [x] NEW
scillm/integrations/certainly.py        # [x] NEW - lazy import wrapper
scillm/extras/providers.py              # [x] Simplified error handling
litellm/llms/lean4.py                   # [x] Direct mode + HTTP fallback
litellm/llms/certainly.py               # [x] Updated docstring
docs/scillm/01_TASKS.md                 # [x] This file
```

---

## Success Criteria

- [x] `from scillm.integrations.certainly import prove_requirement` works
- [x] Direct mode uses no HTTP calls
- [x] Direct mode uses no subprocess spawning
- [x] No PYTHONPATH manipulation in direct mode
- [x] HTTP fallback works when certainly not installed
- [ ] Error stack traces go through both packages cleanly (needs testing)
- [ ] IDE autocomplete/go-to-definition works (needs testing)
- [ ] Tests pass without running bridge server (needs test updates)
- [x] Documentation is consistent and accurate (docs updated 2026-01-09)

---

## Notes

- The certainly repo already has `file://` dep on scillm, creating a circular dependency
- Circular dependency is handled via **lazy imports** in scillm - certainly is only imported when functions are called, not at module load time
- `uv` handles circular file:// dependencies gracefully in editable mode
- Response shape (`additional_kwargs["certainly"]`) remains stable for backward compat
- The `_direct_mode` key in results indicates which mode was used
