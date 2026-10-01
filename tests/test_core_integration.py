"""Optional native Core access must not change the ordinary Flow API."""
from pathlib import Path
from types import SimpleNamespace
import sys
import subprocess

import pytest

from nl_sgtk import core
from nl_sgtk import nl_sgtk as api


def context(root):
    return {"id": 30, "type": "Task", "project_path": str(root),
            "project": {"type": "Project", "id": 10},
            "entity": {"type": "Shot", "id": 20}}


def test_schema0_does_not_import_core_or_open_session(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "nl_core", None)
    assert core.open_core_session(context(tmp_path)) is None


def test_unavailable_storage_is_not_misclassified_as_schema0(tmp_path):
    with pytest.raises(FileNotFoundError, match="storage is unavailable"):
        core.open_core_session(context(tmp_path / "offline"))


@pytest.mark.parametrize("field", [
    "project.Project.sg_project_path", "sg_project_path", "project_path",
])
def test_task_context_path_fields_match_flow_api(tmp_path, field):
    row = context(tmp_path)
    row.pop("project_path")
    row[field] = str(tmp_path)
    assert core.open_core_session(row) is None


def test_task_object_loads_once_and_closes_owned_runtime(monkeypatch):
    closed = []
    session = SimpleNamespace(runtime=SimpleNamespace(
        close=lambda: closed.append(True)))
    opened = []
    monkeypatch.setattr(core, "open_core_session", lambda ctx, **kw:
                        opened.append(ctx) or session)
    with core.Task(context(Path("show"))) as task:
        assert task.id == 30
        assert not opened
        assert task.core_session is session
        assert task.core_session is session
        assert len(opened) == 1
    assert closed == [True]


def test_dictionary_session_is_opt_in_without_mutating_cached_payload(
    tmp_path, monkeypatch,
):
    row = context(tmp_path)
    monkeypatch.setattr(api, "_fetch_entity_context", lambda *args: row)
    opened = []
    marker = object()
    monkeypatch.setattr(core, "open_core_session", lambda ctx, **kw:
                        opened.append(ctx) or marker)
    sg = object()
    assert api.get_task_context(30, sg=sg) is row
    assert not opened
    result = api.get_task_context(30, sg=sg, include_core_session=True)
    assert result["core_session"] is marker
    assert "core_session" not in row


def test_native_session_and_invalid_configuration(tmp_path, monkeypatch):
    import nl_core
    from nl_core.cache import RuntimeCache
    from nl_core.errors import ConfigError

    root = tmp_path / "show"
    nl_core.ProjectInitializer().initialize(
        root, 10, "Show", "SHOW",
        {"primary": nl_core.StorageDefinition("primary", path=str(root))},
        create_project_folders=False)
    row = dict(context(root), content="Comp",
               step={"type": "Step", "id": 40, "name": "Comp"})
    runtime = nl_core.NLCore(
        provider=SimpleNamespace(name="test", fetch_task=lambda _: row),
        cache=RuntimeCache(tmp_path / "cache.db"), discover_provider=False)
    monkeypatch.setattr(nl_core, "NLCore", lambda **kwargs: runtime)
    session = core.open_core_session(row)
    assert session.context.task.id == 30
    assert not session.is_legacy()
    assert not session.path("work").exists()
    # Replacing an accepted native config must never return a schema0 None.
    (root / "000_admin/nl_core/project.toml").write_text("[broken",
                                                       encoding="utf-8")
    config = root / "000_admin/nl_core"
    subprocess.run(["git", "-C", str(config), "add", "project.toml"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(config), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit",
                    "-m", "Invalid configuration fixture"],
                   check=True, capture_output=True)
    with pytest.raises(ConfigError, match="Invalid TOML"):
        core.open_core_session(row)


def test_object_fetch_is_lazy_and_handles_missing_task(monkeypatch):
    monkeypatch.setattr(api, "get_task_context", lambda *a, **k: {"id": 30})
    task = core.get_task(30, sg=object())
    assert task.id == 30
    assert not task._loaded
    monkeypatch.setattr(api, "get_task_context", lambda *a, **k: None)
    assert core.get_task(99, sg=object()) is None
