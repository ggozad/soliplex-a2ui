# soliplex-a2ui

[A2UI](https://a2ui.org/) surfaces over [AG-UI](https://docs.ag-ui.com/)
for pydantic-ai agents.

The payloads follow `@ag-ui/a2ui-middleware`:

- `surface_event(tool_call_id, operations)` builds the `ACTIVITY_SNAPSHOT`
  event, with activity type `a2ui-surface`, that carries a surface's A2UI
  operations to the client. A tool returns it in `ToolReturn.metadata`.
- `apply_user_action(run_input)` appends the user action in
  `forwardedProps.a2uiAction.userAction` to the run's messages as a user
  message named `a2ui_action_context_<hash of the run id>`. Apply it once
  per run.

## Capability

`soliplex_a2ui.capability.A2UICapability` gives a pydantic-ai agent a
`generate_a2ui(description, data)` tool:

```python
from pydantic_ai import Agent
from soliplex_a2ui.capability import A2UICapability

agent = Agent("openai:gpt-5", capabilities=[A2UICapability()])
```

- A separate generating agent writes the surface's components from the
  description, against the A2UI 1.0 basic catalog from `a2ui-core`. Only
  it receives the catalog.
- The surface's data model is `data` exactly as the agent passed it;
  components bind to it with `{"@path": ...}`.
- Every layout passes `a2ui-core`'s strict validation before it is shown.
  An invalid layout is retried up to `max_retries` times (default 3);
  after that the tool paints nothing and tells the agent why.
- The generating agent runs on `model`, or on the calling agent's model
  when `model` is not set, and counts towards the calling run's usage and
  usage limits.
- The tool returns the surface as an `a2ui-surface` activity in
  `ToolReturn.metadata`.

## Web harness

`harness/index.html` is a single page that chats with a soliplex room and
renders its A2UI surfaces with `@a2ui/lit`, loaded from esm.sh. A click on
a surface starts the next run with the action in `forwardedProps`. It is a
test client, not part of the package.

```bash
uv run soliplex-cli serve <installation.yaml> --no-auth-mode   # in soliplex
python -m http.server 8001 --directory harness
```

Open `http://127.0.0.1:8001/index.html`. The query parameters `server`
(default `http://127.0.0.1:8000`) and `room` (default `a2ui`) pick the
soliplex server and room, e.g.
`index.html?server=http://127.0.0.1:8765&room=a2ui`.

## Development

```bash
uv sync
uv run pytest
uv run ruff check
uv run ruff format --check
uvx ty check
```
