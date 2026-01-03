# SCILLM Chat UI (scaffold)

Stack
- React + Vite + TypeScript
- Tailwind + shadcn/ui
- React Query for data fetching

Goals (MVP)
- Chat with model selector (Chutes/codex-agent/Ollama)
- Toggles: strict_json, repair_json, response_format=json_object, temp, max_tokens
- Per-message meta: budget headers, scillm_meta (repaired/retries/error_type), timing
- Inspector (raw/pretty/headers) + health widget

Getting started (planned)
```bash
cd ui/scillm-chat
pnpm install  # or npm/yarn
pnpm dev      # opens Vite dev server
```

Env expected
- SCILLM_UI_API_BASE (defaults to SCILLM proxy)
- SCILLM_UI_CODEWORLD_BASE, SCILLM_UI_LEAN4_BASE, SCILLM_UI_OLLAMA_BASE (optional)
- SCILLM_UI_ENABLE_CODEWORLD / SCILLM_UI_ENABLE_LEAN4 flags

Design tokens
- See `tokens.ts` for palette/spacing/typography tokens.

Next steps
- Add Vite scaffolding and Tailwind config
- Implement three-panel shell (left sessions/settings, center chat, right inspector)
- Wire health widget to existing /models and /healthz endpoints
- Add shadcn components per docs/ui-reference/README.md
