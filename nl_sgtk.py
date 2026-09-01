"""Temporary compatibility shim for legacy version checks and local imports.

This file remains at the repository root so older releases that fetch
`/nl_sgtk.py` can still parse `__version__`. It should be removed once all
supported consumers have migrated to the `src` package layout.
"""

__version__ = "0.11.0"

from src.nl_sgtk import *  # noqa: F401,F403
