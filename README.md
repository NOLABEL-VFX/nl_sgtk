# nl_sgtk

Nolabel's ShotGrid Toolkit helper module. It wraps common ShotGrid Toolkit (SGTK)
authentication flows and provides utility functions for fetching task, shot, asset,
and project metadata.

## Installation

Install from Git (the `requirements.txt` is used automatically by `setup.py`):

```bash
pip install git+https://github.com/NOLABEL-VFX/nl_sgtk.git
```

To install in editable mode:

```bash
pip install -e git+https://github.com/NOLABEL-VFX/nl_sgtk.git#egg=nl_sgtk
```

## Usage

```python
import nl_sgtk

sg, user = nl_sgtk.sgtk_login()
tasks = nl_sgtk.get_user_tasks(user, sg=sg)

task_context = nl_sgtk.get_task_context(task_id=123, sg=sg)
shot_context = nl_sgtk.get_shot_context(shot_id=456, sg=sg)
asset_context = nl_sgtk.get_asset_context(asset_id=789, sg=sg)
project_context = nl_sgtk.get_project_context(project_id=101, sg=sg)

active_projects = nl_sgtk.list_active_projects(sg=sg)
```

## Module Overview

The `nl_sgtk` module provides:

- SGTK authentication helpers (`sgtk_login`, `ensure_sgtk_user`).
- Task metadata retrieval (`get_user_tasks`, `get_task_context`).
- Entity context fetching for shots, assets, and projects.
- ShotGrid URL parsing with `parse_link`.
