"""Publication remains Version-only, including development Projects."""
from nl_sgtk.publisher import published_files_enabled


def test_project_registration_opt_in(monkeypatch):
    monkeypatch.delenv("NL_SGTK_PUBLISHED_FILE_PROJECT_IDS", raising=False)
    assert not published_files_enabled({"id": 4676})
    monkeypatch.setenv("NL_SGTK_PUBLISHED_FILE_PROJECT_IDS", "4676, 99")
    assert not published_files_enabled({"id": 4676})
    assert not published_files_enabled({"id": 467})
    assert not published_files_enabled(None)
