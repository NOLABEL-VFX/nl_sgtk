from __future__ import annotations

from pathlib import Path
import re

from setuptools import setup


ROOT = Path(__file__).parent


def read_requirements(path: Path) -> list[str]:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    return lines


def read_version(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', content, re.M)
    if not match:
        raise RuntimeError("Unable to find __version__ in nl_sgtk.py")
    return match.group(1)


setup(
    name="nl_sgtk",
    version=read_version(ROOT / "nl_sgtk.py"),
    description="Nolabel's ShotGrid Toolkit helpers.",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    author="Nolabel",
    url="https://github.com/nolabel/nl_sgtk",
    py_modules=["nl_sgtk"],
    install_requires=read_requirements(ROOT / "requirements.txt"),
    python_requires=">=3.8",
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
