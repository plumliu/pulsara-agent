---
name: pulsara-mcp-installer
description: Add, edit, authorize, inspect and remove MCP connections through Pulsara's manage_capability tool and shared user forms. Verify a connection through the ordinary MCP list/inspect/use tools.
---

# Pulsara MCP Installer

Use the running Host's `manage_capability` tool for first-party MCP changes.
Use the global `pulsara mcp` CLI only for explicitly out-of-band administration.
Do not use a checkout, private script, raw YAML editing or a second installer.

## Workflow

1. Establish the authoritative MCP endpoint or exact stdio command/args/cwd.
   A vendor CLI is not necessarily an MCP server. Streamable HTTP and explicit
   legacy SSE are supported by the official SDK; never guess transport by name.
2. Reuse USER or WORKSPACE from the user's intent; ask only if it is ambiguous.
   Use `list_capabilities` with `kind:"MCP_SERVER"` before adding a duplicate.
   Copy a target into `inspect_capability` for its location and connection details. Do not silently broaden workspace trust or network access.
3. Call `manage_capability` with ADD_LOCAL_MCP, UPDATE_LOCAL_MCP or REMOVE_LOCAL_MCP,
   exact server_id and scope. Public config uses the native structured schema,
   not a serialized blob. Omit unknown connection details so the shared user
   form can obtain them; do not invent values.
4. Never put credential values in tool arguments, conversation, logs or a package.
   The user fills managed secrets directly in the shared form/private settings.
   Environment references remain an advanced option. OAuth uses AUTHORIZE_MCP
   with explicit user interaction; CLEAR_MCP_AUTHORIZATION clears local grants,
   not remote tokens. A background connection cannot start a browser login.
5. Read both the configuration status and the `adoption` result. Pulsara automatically
   tries to load managed changes before the next model request; do not
   routinely call `reload_capabilities` a second time. Use explicit reload for
   out-of-band CLI/file changes or a reported partial adoption only.
6. List the current server's tools with `list_capabilities`, `kind:"MCP_TOOL"`
   and its copied MCP_SERVER parent. Copy a tool target into `inspect_capability`.
   Read the complete `input_schema`: DIRECT uses the named native tool, META uses
   the returned `tool_ref` with `use_new_mcp_tool`, UNAVAILABLE explains the obstacle.
   Resources and prompts use their respective query kinds; inspect metadata before
   `read_mcp_resource` or `get_mcp_prompt`. Never guess names or schemas.
7. If requested, perform one safe representative call. Saving, authorizing,
   discovering a catalog and executing a tool are distinct outcomes; report
   exactly what passed and what remains unavailable.

## Public candidate example

Every call includes `action` and explicit uppercase `scope`. Minimal calls are:

```json
{"action":"ADD_LOCAL_MCP","scope":"USER","server_id":"docs"}
```

This opens the shared editor for missing connection settings. For updates use
`UPDATE_LOCAL_MCP` with the same `server_id`; omit `config` to edit current truth.
Successful add/update/remove results give the actual `current.config_path`,
not a full configuration echo. Details provide a connection summary, not a complete
UPDATE template; omit config to use the prefilled editor. For removal use
`REMOVE_LOCAL_MCP`. OAuth authorization and clearing local grants
use `AUTHORIZE_MCP` and `CLEAR_MCP_AUTHORIZATION` with `server_id`; include the
exact `plugin_id` only for a Plugin connection. Optional expected guards may be
omitted for fresh native inspection; never guess them. Hook trust and Plugin
enablement are separate actions.

For a known public Streamable HTTP endpoint, `config` can be:

```json
{"display_name":"Docs","enabled":true,"transport":{"type":"streamable_http","endpoint":"https://example.org/mcp"},"auth":{"type":"none"}}
```

Only use `allow_http_localhost: true` inside transport for an explicitly approved
loopback HTTP service. UPDATE replaces the whole entry, not one field. If the
complete current settings are unavailable, omit `config`: the shared editor is
prefilled from current configuration. Do not search repository implementation or
private settings for the schema or secrets.

## User-only import and administration

The capability page can import selected JSON/JSONC MCP configurations without a
model: preview, classify header/env values, fill inputs, save, then test. File
templates require an explicit user-selected file; do not read arbitrary local
files or execute headersHelper commands. Unknown behavior is not silently dropped.

For out-of-band administration, the installed launcher exposes `pulsara mcp list`,
`add`, `doctor`, `enable`, `disable`, and `remove`; inspect `--help`
for supported arguments. Such a CLI process does not own an already running
Host. Adopt its change through GUI refresh or the existing Host
`reload_capabilities` operation when permitted. A successful CLI mutation does
not promise automatic adoption in live sessions.
If the launcher is missing, report the distribution problem instead of using a
repository runtime. Tool availability in the current Host is independent of PATH.

## Boundaries

The skill grants no tool, filesystem, network, subagent or process permission.
Do not remove/replace an existing connection without the user's request.
Do not infer statelessness or read-only effects from one successful call.
Tool calls are not automatically replayed after an auth failure or disconnect.
SDK HTTP/SSE and Pulsara policy are shared by standalone and Plugin MCPs.
