# SciLLM Context — January 19, 2026

Single page hand‑off for agents working on the SciLLM fork. Captures the state after aligning the pi‑mono `scillm` skill and repo docs with the paved path VLM contract.

---

## 1. Current Status (Green)
- **VLM skill parity**: `.pi/skills/scillm/vlm.py` now accepts file paths, HTTPS/file URLs, or existing `data:` URIs. `--inline-remote-images/--inline-remote-timeout` downloads assets when the gateway cannot reach them; `--dry-run` prints the payload without calling Chutes.
- **Dry-run coverage**: `tests/run_vlm_sanity.sh` exercises local, remote-direct, remote-inline, and failure cases entirely offline. This script plus `sanity.sh` are the required pre-flight checks when editing the skill.
- **Docs refreshed**:
  - `docs/scillm/SCILLM_PAVED_PATH_CONTRACT.md` explicitly states that the pi skill mirrors the official VLM path.
  - `docs/scillm/FEATURES.md` lists the new ingestion knobs; `docs/scillm/QUICKSTART.md` shows the dry-run workflow beside the paved preflight helper.
- **Commit pushed**: `d5dfce2727 ("Document scillm VLM paved path alignment")` is on `main` at origin.

---

## 2. How to Verify Quickly
| Scope | Command | Notes |
| --- | --- | --- |
| Skill structure | `.pi/skills/scillm/sanity.sh` | Triggers `uv` sync if needed; ensures `batch`/`vlm` CLIs respond. |
| VLM dry-run coverage | `.pi/skills/scillm/tests/run_vlm_sanity.sh` | Uses dummy CHUTES creds; fails if CLI stops honoring `--dry-run`/inline flags. |
| Repo docs | `rg --line-number --context 3 'inline-remote-images' docs/scillm` | Spot check that docs still point to the current CLI knobs. |

**Live preflight** still requires real `CHUTES_API_BASE` / `CHUTES_API_KEY`. Without them the new `run.sh preflight` helper will fail fast by design.

---

## 3. Environment & Secrets
- Minimum env for live calls:
  - `CHUTES_API_BASE=https://llm.chutes.ai/v1`
  - `CHUTES_API_KEY=cpk_…`
  - `CHUTES_TEXT_MODEL` (text) and `CHUTES_VLM_MODEL` (vision)
- Offline tests:
  - Use the dummy values embedded in `tests/run_vlm_sanity.sh`; they never leave the machine because every invocation passes `--dry-run`.
- Optional knobs:
  - `SCILLM_INLINE_REMOTE_IMAGES=1` (set automatically when the CLI flag is used).
  - `SCILLM_JSON_STRICT=1` (auto when `--json`).

---

## 4. Open Threads / Next Steps
1. **Live VLM sanity**: Once real Chutes creds are available, run `.pi/skills/scillm/run.sh vlm describe path.jpg --json` both with and without `--inline-remote-images` to confirm remote inlining works in production.
2. **Docs drift watch**: `FEATURES.md` and `QUICKSTART.md` now reference the pi skill directly. If the skill path changes, update those references immediately (search for `pi-mono skill dry-runs`).
3. **Pending repo changes**: `git status` shows unrelated staged/unstaged files (`.skills`, `litellm/proxy/common_request_processing.py`, etc.). Coordinate with the owners before touching them—today’s commit only included documentation.

---

## 5. Reference Paths
- Skill CLI: `.pi/skills/scillm/vlm.py` (describe + batch)
- Skill docs: `.pi/skills/scillm/SKILL.md`
- Repo docs touched today:
  - `docs/scillm/SCILLM_PAVED_PATH_CONTRACT.md`
  - `docs/scillm/FEATURES.md`
  - `docs/scillm/QUICKSTART.md`
- Tests: `.pi/skills/scillm/tests/run_vlm_sanity.sh`, `.pi/skills/scillm/sanity.sh`

Keep this file updated whenever we change the paved path or skill contract so incoming agents can pick up without spelunking through history.
