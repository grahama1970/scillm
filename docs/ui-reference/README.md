# SCILLM Chat UI Reference (Patterns to Emulate)

Use these patterns when building the SCILLM chat/workbench. Screenshots to capture (store locally alongside this doc):
- ChatGPT / Gemini: base chat layout, streaming, side drawer settings.
- Claude Artifacts: split code/result + inspector for meta.
- Error states: inline 4xx/5xx chips and expandable details.
- Vision/attachments: small affordance row (Gemini-style).

Layout
- Three panels: left (sessions/settings), center chat, right inspector (collapsible; sheet on mobile).
- Action bar: model select, temp slider, max_tokens, strict_json, repair_json, tool/mode select, new chat.
- Per-message chips: model, latency, retries, repaired/invalid_json, tokens, cost/budget.
- Inspector tabs: Pretty JSON, Raw, Headers/meta (budget, scillm_meta, retries).

SCILLM-specific UI needs
- Budget headers (`x-ratelimit-*`, `x-budget-*`) surfaced in inspector and chips.
- scillm_meta: repaired, retries, error_type, summary counts.
- Modes: CodeWorld (“Strategy”), Lean4 (“Prove”) buttons that trigger separate calls.
- Health widget: redis, arangodb, ollama, codex-agent `/v1/models`, chutes `/models`.
- JSON resilience: strict_json + repair toggles; badge when repaired=true.

Interaction details
- Streaming dots + Stop button.
- Copy buttons for content and raw JSON.
- Keyboard: Ctrl/Cmd+Enter send; Esc closes inspector.
- Sessions stored locally; “New chat” resets context.

Visual system (proposed)
- Typography: Inter (body), JetBrains Mono (code).
- Palette (light):
  - text `#0F172A`, bg `#F8FAFC`, surface `#FFFFFF`, border `#E5E7EB`
  - primary `#2563EB`, accent `#7C3AED`, success `#10B981`, warn `#F59E0B`, error `#EF4444`, muted `#9CA3AF`
- Shadows: subtle `shadow-sm`; elevate inspector/modals only.
- Spacing: 8px grid; chat width max 960px.

Components (shadcn/ui targets)
- Layout: grid with optional Resizable panels; Sheet for inspector on mobile.
- Inputs: Select (model), Slider (temp), Switch (strict/repair), Input (max_tokens, image URL), Textarea (prompt).
- Feedback: Badge (chips), Alert/Toast (errors), Progress (streaming).
- Data: Tabs (Raw/Pretty/Headers), ScrollArea, CodeBlock, Tooltip.
- Health: small Card grid with status dots.

API touchpoints for the UI
- SCILLM proxy (openai_like) `/v1/chat/completions`
- Codex-agent `/v1/models` + chat
- Ollama (optional) `/api/generate` via proxy path
- CodeWorld bridge `/bridge/complete`
- Lean4 bridge `/bridge/units/normalize`
- Health: `/models` (Chutes), `/healthz` (bridges), redis ping helper (small backend) if needed.
