# Review Competition (codex-agent)

Quick start (mini-agent, no Docker):

1) Start/open mini-agent locally if needed (the wrapper will do this):

   - Base URL: `http://127.0.0.1:8788`

2) Run the end-to-end wrapper (defaults DRY_RUN=1):

   - `DRY_RUN=0 make review-run`

3) Outputs:

   - `docs/review_competition/02_gpt5_high.md`
   - `docs/review_competition/03_comparison.md`

Environment knobs:

- `CODEX_AGENT_API_BASE` — override base URL (no `/v1` suffix)
- `REVIEW_MODEL` — force model id exposed by `/v1/models`
- `REVIEW_PROMPT_FILE` — custom prompt file (default: `docs/review_competition/prompt_review.md`)
- `REVIEW_TEMPERATURE` — float (default `0.2`)

Docker sidecar path:

- Bring up the codex sidecar and set `CODEX_AGENT_API_BASE` accordingly.
- If echo mode is disabled, ensure `~/.codex/auth.json` is mounted in the container at `/root/.codex/auth.json` (read-only).

