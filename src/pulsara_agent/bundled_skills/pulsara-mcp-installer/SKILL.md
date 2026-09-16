---
name: pulsara-mcp-installer
description: Add, edit, authorize, test and remove MCP connections through Pulsara's capability management tool and shared user forms.
---

# Pulsara MCP Installer

Use the running Host's `manage_capability` tool for first-party MCP changes.
Use the global `pulsara mcp` CLI only for explicitly out-of-band administration.
Do not use a checkout, private script, raw YAML editing or a second installer.

## Workflow

1. Establish the authoritative MCP endpoint or exact stdio command/args/cwd.
   A vendor CLI is not necessarily an MCP server. Streamable HTTP and explicit
   legacy SSE are supported by the official SDK; never guess transport by name.
2. Choose USER or WORKSPACE from the user's intent. Inspect `list_mcp_servers`
   before adding a duplicate. Do not silently broaden workspace trust or network access.
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
6. Use `list_mcp_servers` for the exact configured identity. When a tool is
   NEW_MCP_META_ONLY, call `inspect_new_mcp_tool` and then `use_new_mcp_tool`
   with the returned reference. Never guess names or schemas; newly discovered tools
   use this route without replacing the current tool list.
7. If requested, perform one safe representative call. Saving, authorizing,
   discovering a catalog and executing a tool are distinct outcomes; report
   exactly what passed and what remains unavailable.

## Public candidate example

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
Host: follow its change with one explicit `reload_capabilities`.
If the launcher is missing, report the distribution problem instead of using a
repository runtime. Tool availability in the current Host is independent of PATH.

## Boundaries

The skill grants no tool, filesystem, network, subagent or process permission.
Do not remove/replace an existing connection without the user's request.
Do not infer statelessness or read-only effects from one successful call.
Tool calls are not automatically replayed after an auth failure or disconnect.
SDK HTTP/SSE and Pulsara policy are shared by standalone and Plugin MCPs.
