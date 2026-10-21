"""Tracker mapping and automatic direct publisher manifest hooks."""
import logging
from types import SimpleNamespace
import uuid

import pytest

from nl_sgtk.publish_manifests import version_snapshot
from nl_sgtk.publisher import ShotgunPublish


@pytest.mark.parametrize('code,expected', [
    ('shot_comp_v005a', (5, 'a', 'v005a')),
    ('shot_comp_V012B.exr', (12, 'b', 'v012b')),
    ('shot_v001a_v006b_render', (6, 'b', 'v006b')),
    ('shot_v003', (3, '', 'v003')),
    ('shot-v002-a', (2, '', 'v002')),
    ('shot_v005abc', (0, '', '')),
    ('v005a', (5, 'a', 'v005a')),
    ('shot.v001.v005a.mov', (5, 'a', 'v005a')),
    ('shot_v005a_v006', (6, '', 'v006')),
    ('shot_v005a_v006abc', (5, 'a', 'v005a')),
    ('versionless', (0, '', '')),
])
def test_variant_identity_matches_provider_catalog(monkeypatch, code, expected):
    from nl_sgtk.provider import NlSgtkProvider

    row = {'id': 42, 'code': code, 'project': {'id': 1},
           'sg_task': {'id': 2}, 'sg_path_to_script': 'fixture.psd'}
    snapshot = version_snapshot(row, 'https://studio.example')
    assert (snapshot['version'], snapshot['variant'],
            snapshot['version_label']) == expected
    provider = NlSgtkProvider()
    sg = SimpleNamespace(find=lambda *args, **kwargs: [row])
    monkeypatch.setattr(provider, '_connection', lambda: (sg, {}))
    context = SimpleNamespace(project=SimpleNamespace(id=1),
        task=SimpleNamespace(id=2), entity=SimpleNamespace(type='Shot', id=3))
    result = provider.find_references(context, {'kind': 'publish'})[0]
    assert (result['version_number'], result['variant'],
            result['version_label']) == expected


def test_legacy_uuid_is_deterministic_and_sources_remain_separate():
    row = {"id": 42, "code": "asset_reference_v012",
           "project": {"id": 1}, "sg_task": {"id": 2},
           "sg_path_to_geometry": "a.abc;b.usd",
           "sg_path_to_script": "c.nk", "sg_path_to_movie": "d.mov",
           "sg_path_to_frames": "e.####.exr"}
    first = version_snapshot(row, "https://studio.example/")
    assert first == version_snapshot(row, "https://studio.example/")
    assert first["publish_uuid"] != version_snapshot(
        row, "https://other.example")["publish_uuid"]
    assert first["sources"]["geometry"] == ["a.abc", "b.usd"]
    assert first["sources"]["script"] == ["c.nk"]
    assert first["sources"]["movie"] == ["d.mov"]
    assert first["sources"]["frames"] == ["e.####.exr"]
    assert first["version"] == 12
    row["sg__publish_uuid"] = str(uuid.uuid4())
    assert version_snapshot(row, "https://studio.example")[
        "publish_uuid"] == row["sg__publish_uuid"]


@pytest.mark.parametrize("manifest_fails", [False, True])
def test_direct_manifest_hook_runs_before_preview_upload_failure(
        monkeypatch, manifest_fails):
    events = []

    def mirror(row, tracker):
        events.append(("manifest", row["id"]))
        if manifest_fails:
            raise OSError("simulated unavailable share")

    def upload(*args):
        events.append(("upload", 42))
        raise OSError("simulated upload failure")

    monkeypatch.setattr("nl_sgtk.publish_manifests.write_core_manifest", mirror)
    publisher = ShotgunPublish.__new__(ShotgunPublish)
    publisher.trusted = True
    publisher.preview = "preview.mov"
    publisher.publish_uuid = str(uuid.uuid4())
    publisher.logger = logging.getLogger(__name__)
    publisher.retrieve_version_info = lambda **kwargs: {"code": "test_v001"}
    publisher._register_publish_state = lambda *args, **kwargs: None
    publisher.sg = SimpleNamespace(
        create=lambda *args: {"id": 42}, upload=upload,
        find_one=lambda *args: None,
        base_url="https://studio.example")
    with pytest.raises(OSError, match="upload failure"):
        publisher.publish()
    assert events == [("manifest", 42), ("upload", 42)]


def test_geometry_only_direct_publish_never_creates_a_version(monkeypatch,
                                                             tmp_path):
    events = []
    publisher = ShotgunPublish.__new__(ShotgunPublish)
    publisher.trusted = True
    publisher.preview = None
    publisher.version = {publisher.GEOMETRY: ["camera.abc"]}
    geometry = tmp_path / "camera.abc"
    geometry.write_bytes(b"fixture")
    publisher.extract_filepaths = lambda: [str(geometry)]
    publisher.retrieve_version_info = lambda **kwargs: {
        "sg__publish_uuid": str(uuid.uuid4())}
    publisher._register_publish_state = lambda status, **kwargs: events.append(status)

    def mirror(row, tracker, *, local_only=False):
        assert local_only
        return "task/.publish-manifests/test.json"

    monkeypatch.setattr("nl_sgtk.publish_manifests.write_core_manifest", mirror)
    # No sg client exists: touching create/upload would fail this test.
    result = publisher.publish()
    assert result["local_only"] is True
    assert "id" not in result
    assert events == ["published_local"]


def test_direct_tracker_retry_reuses_uuid_version(monkeypatch):
    publisher = ShotgunPublish.__new__(ShotgunPublish)
    publisher.trusted = True
    publisher.preview = None
    publisher.publish_uuid = str(uuid.uuid4())
    publisher.version = {"code": "test_v001"}
    publisher.retrieve_version_info = lambda **kwargs: dict(publisher.version)
    publisher._register_publish_state = lambda *args, **kwargs: None
    publisher.published_files_enabled = lambda: False
    publisher.logger = logging.getLogger(__name__)
    publisher.sg = SimpleNamespace(
        find_one=lambda *args: {"id": 42, "code": "original_v001"},
        base_url="https://studio.example")
    monkeypatch.setattr("nl_sgtk.publish_manifests.write_core_manifest",
                        lambda row, tracker: None)
    # No create method: a duplicate create would fail.
    assert publisher.publish(registration="tracker")["id"] == 42


def test_script_registration_without_preview_keeps_validation(monkeypatch):
    publisher = ShotgunPublish.__new__(ShotgunPublish)
    publisher.trusted = False
    publisher.preview = None
    publisher.script_user = publisher.script_key = publisher.user = None
    publisher.publish_uuid = str(uuid.uuid4())
    publisher.version = {field: [] for field in publisher.FILE_FIELDS}
    publisher.version.update({publisher.SCRIPT: ["tracking.nk"],
        "code": "tracking_v001", "description": "Vendor tracking",
        "sg_first_frame": 1001, "sg_last_frame": 1008,
        "project": {"type": "Project", "id": 1},
        "entity": {"type": "Shot", "id": 2},
        "sg_task": {"type": "Task", "id": 3}})
    publisher.retrieve_version_info = lambda **kwargs: dict(publisher.version)
    publisher._register_publish_state = lambda *args, **kwargs: None
    publisher.published_files_enabled = lambda: False
    publisher.logger = logging.getLogger(__name__)
    publisher.sg = SimpleNamespace(find_one=lambda *args: None,
        create=lambda *args: {"id": 42}, base_url="https://studio.example")
    monkeypatch.setattr("nl_sgtk.publish_manifests.write_core_manifest",
                        lambda *args: None)
    with pytest.raises(ValueError, match="No preview"):
        publisher.publish()
    assert publisher.publish(upload_preview=False)["id"] == 42
    publisher.version["sg_task"] = None
    with pytest.raises(ValueError, match="Task is not set"):
        publisher.publish(upload_preview=False)
