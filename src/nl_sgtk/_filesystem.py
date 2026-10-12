"""Reuse optional Core case matching before Linux availability checks."""

from pathlib import Path
import sys


def resolve_storage_case(path: str) -> str:
    """Return a unique existing spelling when a Linux storage path is absent.

    Existing paths and other platforms do not import Core. Without Core the
    original path remains unavailable; dependency and access errors propagate.
    """
    if not sys.platform.startswith("linux") or Path(path).exists():
        return path
    try:
        import nl_core
    except ModuleNotFoundError as error:
        if error.name != "nl_core":
            raise
        return path
    from nl_core.filesystem_paths import resolve_linux_case

    return str(resolve_linux_case(path))
