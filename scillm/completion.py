"""Compat surface for ``scillm.completion`` imports."""

from __future__ import annotations

from . import completion as _completion

__all__ = ["completion"]


def completion(*args, **kwargs):  # type: ignore[override]
    """Delegate to the primary sync completion helper."""

    return _completion(*args, **kwargs)


completion.__module__ = __name__
