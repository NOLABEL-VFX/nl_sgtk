"""Check numeric build ordering without tracker authentication or HTTP."""
import importlib.util
from pathlib import Path

import pytest


def checker():
    path = Path(__file__).parents[1] / "src/nl_sgtk/nl_sgtk_version_check.py"
    spec = importlib.util.spec_from_file_location("build_version_check", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("local,remote,expected", [
    ("1.0.0", "1.0.0.0", False),
    ("1.0.0.0", "1.0.0", False),
    ("1.0.0.2", "1.0.0.10", True),
    ("1.0.0.10", "1.0.1.0", True),
    ("1.0.0.1", "0.12.0", False),
    ("0.12.0", "1.0.0.0", True),
])
def test_update_order(local: str, remote: str, expected: bool) -> None:
    assert checker().is_update_needed(local, remote) is expected
