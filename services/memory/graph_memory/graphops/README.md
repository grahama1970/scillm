# graphops module

Shared graph operations and stores used by operator/automation flows:
- `env.py`: environment helpers for graph ops.
- `schema.py`: schema helpers for graph artifacts.
- `episodes_store.py`: storage helpers for episodes/event streams.
- `reward.py`: reward/score utilities.
These are lower-level than the lessons CLIs; keep stable for callers in training/eval tools.
