"""Check case matching before optional Core and launcher availability gates."""

from pathlib import Path
import sys

import pytest

from nl_sgtk._filesystem import resolve_storage_case
from nl_sgtk.core import open_core_session
from nl_sgtk.provider import NlSgtkProvider


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux filesystem semantics",
)


def test_existing_root_does_not_import_optional_core(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "nl_core", None)
    assert resolve_storage_case(str(tmp_path)) == str(tmp_path)


def test_missing_root_without_core_remains_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "nl_core", None)
    missing = tmp_path / "PROJECTS/123_PROJECT"
    assert resolve_storage_case(str(missing)) == str(missing)
    assert not missing.exists()


def test_optional_core_checks_existing_linux_spelling(tmp_path: Path) -> None:
    actual = tmp_path / "alien/projects/123_PROJECT"
    actual.mkdir(parents=True)
    configured = tmp_path / "ALIEN/PROJECTS/123_PROJECT"
    context = {"task_id": 30, "project_path": str(configured)}
    assert open_core_session(context) is None  # Reachable schema0 root.
    assert not configured.exists()


def test_provider_normalizes_storage_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual = tmp_path / "alien/projects/123_PROJECT"
    actual.mkdir(parents=True)
    configured = tmp_path / "ALIEN/PROJECTS/123_PROJECT"
    provider = NlSgtkProvider()
    monkeypatch.setattr(provider, "_storage_mappings", lambda: [])
    assert provider.localize_launch_path(str(configured)) == str(actual)
    assert not configured.exists()
