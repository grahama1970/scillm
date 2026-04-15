"""Memory agent CLI — package that re-exports sub-app commands."""
from ._helpers import app  # noqa: F401 — main entry point

# Import all sub-modules to register their commands on `app`
from . import core  # noqa: F401
from . import collections  # noqa: F401
from . import scripts  # noqa: F401
from . import query  # noqa: F401
from . import tom  # noqa: F401
from . import sources  # noqa: F401
from . import todos  # noqa: F401
from . import acquire  # noqa: F401
from . import admin  # noqa: F401
from . import explore  # noqa: F401
