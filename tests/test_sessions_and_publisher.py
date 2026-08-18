from __future__ import annotations

from typing import Any, Dict


class CopyableConnection:
    def __init__(self) -> None:
        self.state: Dict[str, Any] = {"requests": []}


def test_sgtk_login_returns_independent_copies(monkeypatch: Any) -> None:
    from nl_sgtk import nl_sgtk as api

    cached_sg = CopyableConnection()
    cached_user = {"id": 1, "metadata": {"role": "artist"}}
    monkeypatch.setattr(api, "_sgtk_login_cached", lambda **kwargs: (cached_sg, cached_user))

    first_sg, first_user = api.sgtk_login()
    second_sg, second_user = api.sgtk_login()

    assert first_sg is not cached_sg
    assert second_sg is not cached_sg
    assert first_sg is not second_sg
    assert first_user is not second_user
    first_sg.state["requests"].append("first")
    first_user["metadata"]["role"] = "lead"
    assert second_sg.state["requests"] == []
    assert second_user["metadata"]["role"] == "artist"


def test_published_file_payload_uses_shotgrid_path_dictionary() -> None:
    from nl_sgtk.publisher import ShotgunPublish

    publisher = ShotgunPublish.__new__(ShotgunPublish)
    publisher.context = {
        "project": {"type": "Project", "id": 1},
        "entity": {"type": "Shot", "id": 2},
        "task": {"type": "Task", "id": 3},
    }
    publisher.published_file_types = []

    request = publisher.publish_request_from_file(
        "C:/show/shot/render_v003.exr",
        {"type": "Version", "id": 10, "description": "Render"},
    )

    assert request["data"]["path"] == {
        "local_path": "C:/show/shot/render_v003.exr"
    }
    assert request["data"]["sg_path_string"] == "C:/show/shot/render_v003.exr"


def test_published_files_feature_flag_is_disabled() -> None:
    from nl_sgtk.publisher import published_files_enabled

    assert published_files_enabled() is False
    assert published_files_enabled({"sg_enable_published_files": True}) is False
