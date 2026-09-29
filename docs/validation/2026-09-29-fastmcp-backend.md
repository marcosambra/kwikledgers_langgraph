# FastMCP Backend Validation

Date: 2026-09-29.

## Scope

Implemented `fastmcp_client.py`, `fastmcp_adapters.py`, all six selectors in
`backend_factory.py`, and focused backend tests. The legacy default is unchanged.
State, runtime, lifecycle, mutable flow, issuer, server catalog and legacy files
were not edited. Other contributors' changes remain outside the scoped commit.

## Public Interface

- `FastMCPClientConfig.from_environment()` configures stdio or Streamable HTTP.
- `FastMCPClient.call(capability_name, arguments)` is the synchronous seam.
- `FastMCPClient.acall(capability_name, arguments)` is the asynchronous seam.
- Inject a `CapabilityClient` through the optional `client` factory argument.
- All six factories support `backend_name="fastmcp"`; no connection opens while
  constructing ports. Daily ports share a single client.
- `fetch_daily_summary()` and `fetch_sprint_stories_detailed(request)` are reads.
- `preview_story_branch_association(request)` / `associate_story_branch(request)`.
- `preview_story_pull_request(request)` / `create_story_pull_request(request)`.
- `preview_story_status_transition(request)` / `update_story_status(request)`.
- `close_task(request)` calls only `update_work_item_effort`, once, atomically.
- `sync_daily_tracking(summary, source, *, execution_capability=None)` and
  `send_daily_summary_notification(summary, *, execution_capability=None)` accept
  explicit internal tokens. The optional factory `execution_capability_provider`
  callback also receives `(capability_name, exact_token_free_arguments)`.
- `technical_arguments_for_capability(capability_name, request)` exposes the
  exact token-free argument mapping for the issuer. No adapter signs tokens.

## Transport Contract

The MCP SDK owns both transports and `ClientSession` initialization. Tool calls
retain the current gateway shape:

```python
await session.call_tool(capability_name, arguments={"arguments": atomic_arguments})
```

Structured content and JSON text are normalized to dictionary data, unwrapping
the gateway `{capability, domain, operation, data}` envelope. Invalid JSON, wrong
capabilities, non-object data and tool errors fail without exposing response text,
arguments or tokens in client exception messages.

Timeout covers transport opening, initialization and the tool call; session and
transport contexts are closed on cancellation. Execution is never automatically
retried after timeout or transport failure because its outcome can be unknown.
Calling the sync seam inside an active event loop runs a separate worker/loop
and preserves context variables. It still blocks its caller; async callers that
need responsiveness should await `acall`.

Environment variables:

| Variable | Default |
| --- | --- |
| `KWIKLEDGERS_LANGGRAPH_BACKEND` | `legacy` |
| `KWIKLEDGERS_FASTMCP_TRANSPORT` | `http` when URL is set, otherwise `stdio` |
| `KWIKLEDGERS_FASTMCP_COMMAND` | Current Python executable |
| `KWIKLEDGERS_FASTMCP_ARGS` | JSON array `["-m", "kwikledgers_fastmcp.server"]` |
| `KWIKLEDGERS_FASTMCP_URL` | Required for HTTP |
| `KWIKLEDGERS_FASTMCP_TIMEOUT_SECONDS` | `30` |

Stdio explicitly inherits the application environment, including the execution
key, because the SDK's default environment strips application variables. Supply
`FastMCPClientConfig(env={...})` for an explicit environment. Only configure trusted
local commands/endpoints. The default command requires the server package to be
installed in that interpreter; alternatively select its interpreter and arguments
explicitly. The client does not import or directly dispatch server handlers.

## Main Integration Required

Main owns the remaining graph/issuer/server contracts, not this backend slice.

1. Implement `execution_capability_for_state(state, capability_name, arguments)`
   and authorize the exact output of `technical_arguments_for_capability`, not
   the original workflow request. Insert `execution_capability` only after signing.
2. Map approved branch and PR execution to `associate_story_branch` and
   `create_story_pull_request`. Preview calls omit tokens and workflow fields;
   execute calls may retain `expected_revision` / `expected_revisions`.
3. Map story status to `update_story_status` with exactly `story_id`, `status`
   from `target_status`, and integer `expected_revision` from the approved
   `preview.story_revision` or matching explicit request revision. Never sign
   `target_status` or the `preview` object as server arguments.
4. Map task closure to `update_work_item_effort` with `work_item_id`, approved
   `completed_work_hours`, `status="Closed"`, and integer `expected_revision`.
   Optional `comment` and non-null `remaining_work_hours` come from the preview;
   absent/null remaining work is omitted, never forced to zero. Carry the native
   task revision into the approved preview (`task_revision`, `revision`, or
   `expected_revision`) or explicit request. Mismatching revisions fail locally.
5. Extend the existing native `update_work_item_effort` service with optional
   `status`, applying effort, Closed and revision test in one work-item patch.
   Reject unsupported approved effort fields before patching. Do not create a
   new catalog name or fall back to separate effort/status calls.
6. Return structured native `get_story_details` data containing story ID,
   status, title/type/URL and native positive integer `revision` (or `rev` /
   `story_revision`). Azure SDK-shaped `fields` are also accepted. Text-only
   details and synthetic revisions deliberately fail closed. In the inspected
   server checkout, details were still text-only and effort lacked `status`.
7. Issue narrowly scoped internal tokens in daily graph nodes for
   `sync_daily_tracking` and `send_notification`, over their exact mapped
   arguments. Pass the explicit keyword token or a trusted issuer callback.
   Do not authorize arbitrary Azure mutations through this internal allowance.
8. Update the obsolete CLI test
   `test_run_daily_summary_rejects_fastmcp_backend_while_paused` to exercise
   supported FastMCP construction with an injected client rather than rejection.
9. Declare/lock the compatible MCP SDK dependency in the main-owned package
   metadata. Contrary to the supplied context, the inspected project metadata
   did not contain it. Validation installed `mcp==1.30.0` locally without editing
   dependency files. This client uses the MCP 1.x transport API (`mcp>=1.10,<2`).

## Evidence

Focused validation:

```bash
cd /home/ambra/Kwikledgers/kwikledgers_langgraph
poetry run python -m unittest discover -s tests -p 'test_*backend*.py' -v
```

Result: 29 tests passed, including real local SDK servers using stdio and HTTP,
nested arguments, token-free previews, exact execute payloads, native revision
guards, atomic closure, empty remaining work, disabled notifications, context
propagation, timeout cleanup and safe client exception messages.

The full discovery run before the final environment-forwarding test executed
172 tests: 171 passed and one errored because the old CLI test still expects a
paused FastMCP backend, then launches a server absent from the LangGraph venv.
This test is outside the authorized file slice and was not changed. No real
Azure request or mutation was used by the focused integration tests.

Final regression validation selected the complete discovered suite except
`test_daily_summary_cli.DailySummaryCliTestCase.test_run_daily_summary_rejects_fastmcp_backend_while_paused`:
172 tests passed; exactly one obsolete assertion was excluded. This is not a
claim that unfiltered discovery is green. Main must replace that assertion.

Editor diagnostics reported no errors in the five code/test files. The SDK
emits a deprecation warning for its 1.x `streamablehttp_client` compatibility API;
the HTTP integration itself passes. Production gateway/native Azure operation
remains dependent on the main-owned contracts above, not proven by local SDK
fixture tests. Engineering subagents were not available in this session.

## Observability And Upgrade Notes

Existing graph events and `observability.backend_name` remain authoritative;
this backend adds no competing workflow spans, tracking state or approval rules.
Transport failures raise a safe `FastMCPClientError` for graph-owned handling.
Do not log execution tokens, signing keys, request payloads or subprocess
environments. Future transport metrics should use stable capability/transport
labels only, with correlation supplied through existing graph context.

An MCP 2.x upgrade must revisit transport imports/options and response types and
rerun the real stdio/HTTP tests; installing unconstrained MCP 2.2.0 did not expose
the 1.x `streamablehttp_client` API. Session pooling is intentionally absent to
avoid binding sessions to a different worker event loop.