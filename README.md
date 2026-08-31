# nl_sgtk

Nolabel's ShotGrid Toolkit helper module. It wraps common ShotGrid Toolkit (SGTK)
authentication flows and provides utility functions for fetching task, shot, asset,
and project metadata.

## Installation

Install from Git:

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

Create a technical Ticket for Pipeline Development:

```python
from nl_sgtk import TicketPriority, TicketType, create_ticket, pipeline_group

result = create_ticket(
    "Nuke render failed",
    "The render process stopped before frame 1008.",
    ticket_type=TicketType.ERROR,
    priority=TicketPriority.HIGH,
    user_group=pipeline_group.PIPELINE,
    metadata={
        "file": "shot010_comp_v003.nk",
        "frame": 1008,
        "application": "Nuke",
    },
    attachments=["C:/temp/render_diagnostic.txt"],
)
print(result.ticket_id)
```

Use `pipeline_group.COMFY`, `pipeline_group.MAX`, or
`pipeline_group.HOUDINI` to route respectively to ComfyUI Development, 3DMax
Development, or Houdini Development. Callers may alternatively pass a complete
ShotGrid Group or HumanUser entity dictionary to `user_group`.
Enum routing uses the stable ShotGrid Group ID and reads the current Group name
dynamically, so a later rename does not break reporting.

See [REPORT_API.md](REPORT_API.md) for the compact reporting API reference.

## Module Overview

The `nl_sgtk` module provides:

- SGTK authentication helpers (`sgtk_login`, `ensure_sgtk_user`).
- Task metadata retrieval (`get_user_tasks`, `get_task_context`).
- Entity context fetching for shots, assets, and projects.
- ShotGrid URL parsing with `parse_link`.
- ShotGrid publishing through `ShotgunPublish`.
- Typed technical Ticket creation through `nl_sgtk.tickets`.

## Future Additions

- Shared schema validation for Version, PublishedFile, Task, Shot, Asset, and Project payloads.
- A typed context object that can round-trip between ShotGrid URLs, dictionaries, JSON, and environment variables.
- First-class publish templates for common DCC outputs such as scripts, cameras, flipbooks, geometry, renders, and plates.
- Dependency graph helpers for resolving upstream/downstream publishes and detecting stale dependencies.
- Batch publish transactions with dry-run output, rollback guidance, and clearer per-file error reporting.
- Centralized path-sequence utilities so frame patterns, movie paths, storage remaps, and source-path comparisons behave consistently.
- Optional upload helpers for thumbnails, filmstrips, notes, playlists, and review-session metadata.
- Test fixtures and fake ShotGrid clients for validating publisher behavior without a live ShotGrid connection.
