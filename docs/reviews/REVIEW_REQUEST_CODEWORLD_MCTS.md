# CodeWorld: MCTS Strategy PR (Phase 1)

Fork: https://github.com/grahama1970/codeworld
Branch: feat/mcts-strategy
Patch: patches/codeworld-mcts.patch (attached in SciLLM repo)

## Summary
- Add minimal root-bandit MCTS for variant selection (no code exec in rollouts)
- Deterministic seeding via `SCILLM_DETERMINISTIC_SEED` or `strategy_config.seed`
- Bridge detects `provider.args.strategy == "mcts"` (or `strategy_config.name`)
- Response attaches `results[i].mcts` and `run_manifest.mcts_stats`

## Files
- src/codeworld/engine/mcts.py (new)
- src/codeworld/bridge/server.py (strategy branch + manifest fields)

## Testing
- Unit: seeded run_mcts returns stable best_variant and visits distribution
- Integration: `/bridge/complete` with 3 variants, seeded; assert presence of `mcts` and manifest fields

## Security & Determinism
- No new code execution in rollouts
- Determinism requires seed; recorded in results
- Future partial-eval behind `CODEWORLD_MCTS_EXEC=1` (not in this patch)

## Open Questions
- Should “no variants” return 400 vs 422?
- Any schema preference for `strategy_config` validation (pydantic vs loose dict)?

