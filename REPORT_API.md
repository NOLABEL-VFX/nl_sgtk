# Technical Ticket Reporting API

`nl_sgtk.tickets` lets pipeline tools report technical bugs, errors, and
requests to the appropriate ShotGrid support group without depending on a DCC
or Qt. It authenticates the current user, validates the live Ticket schema,
creates and reads back the Ticket, and then uploads any attachments.

```python
from nl_sgtk import (
    TicketPriority,
    TicketType,
    create_ticket,
    pipeline_group,
)

result = create_ticket(
    topic="Nuke render failed",
    content="The render stopped before frame 1008.",
    ticket_type=TicketType.ERROR,
    priority=TicketPriority.HIGH,
    user_group=pipeline_group.PIPELINE,
    metadata={
        "application": "Nuke",
        "file": "shot010_comp_v003.nk",
        "frame": 1008,
    },
    attachments=["C:/temp/render_diagnostic.txt"],
)

print(result.ticket_id)
```

## Available options

- Types: `TicketType.BUG`, `FEATURE`, `SOFTWARE_NEED`, `DATA_WRANGLING`.
  `ERROR` is an alias for `BUG`; `REQUEST` is an alias for `FEATURE`.
- Priorities: `TicketPriority.LOW`, `MEDIUM`, `HIGH`, `URGENT`, `CRITICAL`.
- Groups: `pipeline_group.PIPELINE`, `COMFY`, `MAX`, `HOUDINI`.
  They route respectively to Pipeline, ComfyUI, 3DMax, and Houdini
  Development.
- `user_group` may instead be a complete ShotGrid `Group` or `HumanUser`
  dictionary containing `type` and `id`.
- `metadata` accepts structured diagnostic values and is rendered above the
  content with reporter and UTC occurrence information.
- `attachments` accepts existing local file paths.
- Tests or host applications may inject paired `sg` and `user` arguments;
  otherwise the API calls `sgtk_login()` for the current user.

The function returns `TicketResult`. Validation and schema errors happen
before creation. If readback or an attachment fails after creation, the raised
exception retains the created `ticket_id`; inspect that Ticket before retrying.
