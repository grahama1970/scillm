# Units Collaboration (Lean4 / Certainly)

Default policy: `ask_always` — the bridge will always ask for unit confirmation before formalization. No assumptions.

## Fields (minimal, high‑value)
- `airspeed` (preferred `m/s`, accepts `kn`)
- `altitude` (preferred `m`, accepts `ft`)
- `pressure` (preferred `Pa`, accepts `kPa`, `psi`, `psf`)
- `temperature` (preferred `K`, accepts `degC`, `degF`)

## Flow
1. POST `/bridge/complete` with `engineering` → 422 `clarification_needed` with:
   - `human_prompt` (one sentence, copy/paste JSON example)
   - `questions[]` (per‑field recommendations)
   - `canonical_si_preview` (SI conversions for parsable inputs)
2. Re‑POST including `engineering_confirmed: true` → run proceeds.

Preview without running Lean4:
```bash
POST /bridge/units/normalize
{ "engineering": { "airspeed": {"value": 250, "unit": "kn"} } }
```

## Policies
- `ask_always` (default): always ask once, even if values parse.
- `require`: ask when missing/ambiguous/out‑of‑range; never auto‑convert.

Set at runtime: `LEAN4_UNITS_POLICY=ask_always`.

## Rationale
- Defense/aerospace demands traceability; the agent must ask the human.
- Minimal fields, tiny accepted sets → fewer surprises, less brittleness.
- Manifest records: normalized SI values, confirmation flag, unit‑defs hash.

