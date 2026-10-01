"""Optional, lazy nl_core sessions for native project Tasks.

Importing this module does not import nl_core or contact Flow. Core remains
an optional dependency; schema0 Tasks expose no session through this API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from nl_core import TaskSession


def open_core_session(
    context: Mapping[str, Any], *, sg: Any = None,
    user: Optional[Mapping[str, Any]] = None,
) -> Optional["TaskSession"]:
    """Open a validated native Task; return None only for schema0.

    ``context`` must be a verified ``get_task_context`` result. The returned
    session owns a runtime: close ``session.runtime`` when finished.
    Configuration, identity, import and provider errors propagate.
    """
    from .nl_sgtk import get_storages, verify_path

    number = context.get("task_id") or context.get("id")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        raise ValueError("A verified positive Task ID is required")
    project_path = (
        context.get("project.Project.sg_project_path")
        or context.get("sg_project_path") or context.get("project_path")
        or (context.get("project") or {}).get("sg_project_path"))
    if not project_path:
        raise ValueError("The Task has no verified project path")
    if sg is not None:
        project_path = verify_path(str(project_path), get_storages(sg=sg))
    root = Path(project_path).expanduser()
    if not root.is_dir():
        raise FileNotFoundError("Project storage is unavailable: %s" % root)
    if not (root / "000_admin/nl_core/project.toml").is_file():
        return None

    import nl_core
    from .provider import NlSgtkProvider

    runtime = nl_core.NLCore(
        provider=NlSgtkProvider(sg=sg, user=user), discover_provider=False)
    try:
        session = runtime.open_task(
            number, project_root=root,
            refresh=nl_core.RefreshPolicy.FORCE_REFRESH)
        error = session.context.metadata.refresh_error
        if error:
            raise RuntimeError(error)
        if session.is_legacy():
            raise RuntimeError("Native configuration changed during Task open")
        for name in ("project", "entity"):
            expected = context.get(name) or {}
            actual = getattr(session.context, name)
            if (expected.get("id") != actual.id
                    or expected.get("type") != actual.type):
                raise ValueError("Core %s does not match the Task" % name)
        return session
    except BaseException:
        runtime.close()
        raise


@dataclass
class Task:
    """A Flow Task with a lazily opened optional native Core session."""

    context: Dict[str, Any]
    sg: Any = field(default=None, repr=False)
    user: Optional[Mapping[str, Any]] = field(default=None, repr=False)
    _session: Optional["TaskSession"] = field(default=None, init=False,
                                             repr=False)
    _loaded: bool = field(default=False, init=False, repr=False)

    @property
    def id(self) -> int:
        """Return the verified Task ID."""
        return int(self.context.get("task_id") or self.context["id"])

    @property
    def core_session(self) -> Optional["TaskSession"]:
        """Return the native session, or None for a schema0 project."""
        if not self._loaded:
            self._session = open_core_session(
                self.context, sg=self.sg, user=self.user)
            self._loaded = True
        return self._session

    def close(self) -> None:
        """Release the owned runtime; later access reopens fresh policy."""
        if self._session is not None:
            self._session.runtime.close()
        self._session = None
        self._loaded = False

    def __enter__(self) -> "Task":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def get_task(task_id: int, sg: Any = None) -> Optional[Task]:
    """Fetch a Flow Task object without opening Core until requested."""
    from .nl_sgtk import get_task_context, sgtk_login

    user = None
    if sg is None:
        sg, user = sgtk_login()
        if sg is None:
            raise ConnectionError("nl_sgtk could not authenticate")
    context = get_task_context(task_id, sg=sg)
    return Task(context, sg=sg, user=user) if context is not None else None
