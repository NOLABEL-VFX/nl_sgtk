"""Verify normalized, project-local Prepro library context."""

import pytest

from nl_sgtk.provider import NlSgtkProvider


class Shotgun:
    def __init__(self, links, library_project=10):
        self.links = links
        self.library_project = library_project

    def find_one(self, entity, filters, fields):
        if entity == "CustomThreadedEntity01":
            return {"id": 20, "code": "General", "sg_type_2_0": self.links,
                    "project": {"type": "Project", "id": 10}}
        if entity == "CustomEntity08":
            return {"id": 50, "code": "Scripts",
                    "project": {"type": "Project",
                                "id": self.library_project}}
        return {}


def fetch(monkeypatch, links, library_project=10):
    from nl_sgtk import nl_sgtk
    monkeypatch.setattr(nl_sgtk, "get_task_context", lambda *args, **kwargs: {
        "id": 30, "content": "Script",
        "project": {"type": "Project", "id": 10, "name": "Show"},
        "entity": {"type": "CustomThreadedEntity01", "id": 20,
                   "name": "General"},
    })
    return NlSgtkProvider(sg=Shotgun(links, library_project)).fetch_task(30)


def test_single_library_survives_core_normalization(monkeypatch):
    from nl_core.providers import normalize_task_payload
    payload = fetch(monkeypatch, [{"type": "CustomEntity08", "id": 50}])
    assert payload["prepro"] == {
        "name": "general", "library": "scripts", "library_id": 50}
    assert normalize_task_payload(payload, "nl_sgtk").fields["prepro"] == (
        payload["prepro"])


@pytest.mark.parametrize("links", [[], [{"id": 50}, {"id": 51}]])
def test_missing_or_ambiguous_library_has_no_guess(monkeypatch, links):
    assert "prepro" not in fetch(monkeypatch, links)


def test_cross_project_library_is_rejected(monkeypatch):
    with pytest.raises(ValueError, match="another project"):
        fetch(monkeypatch, [{"id": 50}], library_project=11)
