from types import SimpleNamespace as NS
from unittest.mock import Mock

from nl_sgtk.provider import NlSgtkProvider


def test_version_references_keep_project_entity_and_step_scope():
    provider = NlSgtkProvider()
    provider._sg = Mock()
    provider._sg.find.return_value = [{"id": 10, "code": "shot_roto_v002",
        "sg_path_to_frames": "/project/roto.####.exr", "sg__publish_uuid": "u"}]
    context = NS(project=NS(id=1), entity=NS(type="Shot", id=2), task=NS(id=3))
    rows = provider.find_references(context, {"kind": "version", "step": "RTO"})
    args = provider._sg.find.call_args.args
    assert args[0] == "Version"
    assert args[1][:2] == [["project", "is", {"type": "Project", "id": 1}],
                           ["entity", "is", {"type": "Shot", "id": 2}]]
    assert rows[0]["version_number"] == 2
    assert rows[0]["path"] == "/project/roto.####.exr"
    provider.find_references(context, {"kind": "publish"})
    assert ["sg_task", "is", {"type": "Task", "id": 3}] in provider._sg.find.call_args.args[1]
