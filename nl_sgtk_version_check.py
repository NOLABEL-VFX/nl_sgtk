from __future__ import annotations

import re
import sys

import urllib3


RAW_URL = "https://raw.githubusercontent.com/NOLABEL-VFX/nl_sgtk/main/nl_sgtk.py"
UPDATE_COMMAND = ["pip", "install", "--force-reinstall", "git+https://github.com/NOLABEL-VFX/nl_sgtk"]

http = urllib3.PoolManager()


def parse_version_string(version_string: str) -> tuple[int, ...]:
    """
    Turns '0.1.10' into (0, 1, 10)
    """
    return tuple(int(part) for part in version_string.strip().split("."))


def read_remote_version() -> str:
    response = http.request(
        "GET",
        RAW_URL,
        timeout=urllib3.Timeout(connect=5.0, read=10.0),
    )

    if response.status != 200:
        raise RuntimeError(f"Failed to fetch remote file. HTTP {response.status}")

    text = response.data.decode("utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise RuntimeError("Could not find remote __version__")
    
    return match.group(1)


def is_update_needed(local_version: str, remote_version: str) -> bool:
    return parse_version_string(remote_version) > parse_version_string(local_version)


def check_for_update(current_version: str) -> dict[str, str | bool]:
    remote_version = read_remote_version()
    return {
        "local_version": current_version,
        "remote_version": remote_version,
        "update_available": is_update_needed(current_version, remote_version),
    }


def notify_if_update_available(current_version: str) -> None:
    result = check_for_update(current_version)

    if not result["update_available"]:
        return

    command = f'""{sys.executable}" -m {' '.join(UPDATE_COMMAND)}"'
    print(
        "[notice] There is an update to nl_sgtk module "
        f"{result['local_version']} >> {result['remote_version']}. "
        f"Run {command} to update."
    )

def get_update_command():
    command = [sys.executable, "-m"] + UPDATE_COMMAND
    return command


