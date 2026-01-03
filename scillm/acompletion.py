"""Compat surface for ``scillm.acompletion``.

Extractor/DevOps paved-path scripts import ``scillm.acompletion`` directly
to guard against local shims.  Older wheels only shipped the top-level
function on ``scillm.__init__`` which broke those imports.  This module is a
thin alias around the canonical helper so ``from scillm import acompletion``
and ``import scillm.acompletion`` are both satisfied without bespoke hacks.
"""

from __future__ import annotations

from . import acompletion as _acompletion

__all__ = ["acompletion"]


async def acompletion(*args, **kwargs):  # type: ignore[override]
    """Delegate to the paved-path async completion helper."""

    return await _acompletion(*args, **kwargs)


# Re-export for attr-based access, e.g. ``scillm.acompletion.acompletion``.
acompletion.__module__ = __name__
