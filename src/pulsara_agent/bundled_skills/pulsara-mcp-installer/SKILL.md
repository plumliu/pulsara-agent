---
name: pulsara-mcp-installer
description: Set up MCP connections from authoritative endpoints or commands, configure authentication and verify availability, or diagnose connection failures. Use for setup and troubleshooting workflows; known-target queries, toggles, removal and OAuth actions use the capability tools directly.
---

# Pulsara MCP Installer

## Set up a connection

Use ROOT-native `manage_capability` with the intended configuration file's actual
absolute path and a new independent `server_id`:

```json
{"target":{"kind":"MCP_CONNECTION","config_path":"/actual/project/.pulsara/mcp.yaml","server_id":"docs"},"action":"ADD_LOCAL_MCP"}
```

Omitting `config` opens the shared connection editor. Existing independent and
Plugin connections use `UPDATE_MCP_CONNECTION` with their observed Target;
omission opens the prefilled editor. A supplied object patches only explicit
public settings, preserving the rest. Plugin `config:null` removes the whole
overlay, including its scope override; package declarations remain package-owned.
New independent and Plugin connections default to ROOT/subagent use; users can
restrict this in the capability page.

1. Establish the vendor's exact endpoint or stdio command/args/cwd. A vendor CLI
   is not necessarily an MCP server. The official SDK supports Streamable HTTP
   and explicit legacy SSE; do not guess transport from a product name.
2. Reuse `USER` or `WORKSPACE` from the user's intent; ask only if ambiguous.
   Read the current session context: a quick/transient session has no project
   scope. Use `USER` for setup there; do not query or manage `WORKSPACE`, or treat
   its temporary working directory as a project. If the user explicitly wants
   project configuration, ask them to open the intended project first.
   List `kind:"MCP"` to avoid duplicates. Inspect an observed target for
   the connection location and details; copy its `MCP_CONNECTION` target into management.
3. Use the editor for unknown settings or credentials. Never put secret values
   in arguments, conversation, logs or packages. Do not read private settings or
   repository implementation to construct config, or broaden trust/network access.
4. For an already configured OAuth connection use `AUTHORIZE_MCP` with its
   `MCP_CONNECTION` target. The user authorizes interactively.
   `CLEAR_MCP_AUTHORIZATION` clears local grants, not remote tokens. A background
   connection cannot initiate browser login.

Read `references/connection-configuration.md` only when supplying
public config or handling user-only import/out-of-band administration.

## Verify and recover

Enabled and approved connections discover their catalogs in the background.
Saving, OAuth authorization, directory discovery and a successful call are
separate outcomes. Copy observed Targets unchanged: the configured service key
can differ from the runtime `server_id` in tool/resource Targets.

A capability-page result notice can be acknowledged directly. Do not repeat its
action or query merely to confirm it; inspect the exact Target for a concrete
failure or requested diagnosis.

Distinguish a missing executable or unreachable service from missing config.
Valid config still uses UPDATE_MCP_CONNECTION or REMOVE_LOCAL_MCP; confirmed absent
config can be recreated with ADD_LOCAL_MCP. Unparseable existing config may block
management: report the specific error; a parse failure does not prove that an
entry is absent or a previous save failed. Do not infer who changed the file or
how it became malformed. Do not propose clearing or rewriting managed YAML as
a repair shortcut; explain that malformed existing config needs explicit
configuration recovery outside this management path. Do not bypass the owner with raw YAML
writes or claim private credentials were cleaned. Diagnosis alone does not
authorize deleting or recreating a connection.

Read `meta.status` and actual `content`. ERROR may still describe a completed
change followed by cleanup or refresh failure. Address the failed step without
repeating a confirmed mutation. ROOT-native `reload_capabilities({})` is for
incomplete adoption or out-of-band changes, not a routine second step.

Use `query_capabilities` with `kind:"MCP"`: no target lists services; an observed
MCP_CONNECTION target lists that service's tools; an MCP_TOOL target returns its
interface. Exact queries use only target and kind. Optional list `query` searches
tools/services; tool hits already include the full interface and need no second
query. List filters may include scope or a PLUGIN parent, not a service parent.

Read the actual `input_schema`, any `output_schema`, and `invocation`. META uses
`call_mcp_tool` with the copied `invocation.target` and schema-conforming arguments;
UNAVAILABLE explains the obstacle. MCP_TOOL_REF is shared by native calls and
applicable Cells in the same session. Do not guess names, references or schemas.

For an unknown interface, receive its native query result or explicitly output
it from a preparation Cell before writing a business Cell. A schema only in JS
memory has not been read by the model. Already known interfaces can be reused.
`output_schema` describes successful structuredContent, not the full result or
failures. Connection management has no JS API.

For resources, `read_mcp_resource` takes an MCP_CONNECTION to list resources and
templates, or an MCP_RESOURCE with the returned server_id and concrete uri to
read one. Fill a template's uri_template using its actual service identifier.
Prompts stay in the user's composer slash menu. Native NEXT reads the frozen
result through read_file; it does not repeat a query or remote call.

Perform a representative call only when requested and appropriate; report what
passed and what remains unavailable. Authentication failures or disconnects do
not authorize replaying tool calls. A successful call does not establish
statelessness or read-only effects. Ordinary tool, process and network permissions
apply to both independent and Plugin connections.
