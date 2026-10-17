---
name: pulsara-mcp-installer
description: Set up MCP connections from authoritative endpoints or commands, configure authentication and verify availability, or diagnose connection failures. Use for setup and troubleshooting workflows; known-target queries, toggles, removal and OAuth actions use the capability tools directly.
---

# Pulsara MCP Installer

## Set up a connection

Use `manage_capability` with the user's scope and a new independent `server_id`:

```json
{"action":"ADD_LOCAL_MCP","scope":"USER","server_id":"docs"}
```

Omitting `config` opens the shared connection editor. For an existing independent
connection use `UPDATE_LOCAL_MCP` with its observed scope and ID; omission opens
its prefilled editor. A supplied config replaces the complete entry, not selected
fields. The editor also controls enablement. Plugin connections instead use
`CONFIGURE_PLUGIN_MCP_CONNECTION` with `plugin_id` and the package-local `server_id`;
omit `overlay` for that editor. Package declarations remain package-owned.

1. Establish the vendor's exact endpoint or stdio command/args/cwd. A vendor CLI
   is not necessarily an MCP server. The official SDK supports Streamable HTTP
   and explicit legacy SSE; do not guess transport from a product name.
2. Reuse `USER` or `WORKSPACE` from the user's intent; ask only if ambiguous.
   List `kind:"MCP_SERVER"` to avoid duplicates. Inspect an observed target for
   the connection location and details; copy identity fields into management.
3. Use the editor for unknown settings or credentials. Never put secret values
   in arguments, conversation, logs or packages. Do not read private settings or
   repository implementation to construct config, or broaden trust/network access.
4. For an already configured OAuth connection use `AUTHORIZE_MCP` with `server_id`;
   add `plugin_id` for a Plugin connection. The user authorizes interactively.
   `CLEAR_MCP_AUTHORIZATION` clears local grants, not remote tokens. A background
   connection cannot initiate browser login. Optional guards can normally be omitted.

Read `references/connection-configuration.md` only when supplying a complete
public config or handling user-only import/out-of-band administration.

## Verify and recover

Capability-page actions may produce a brief Runtime result notice. Use its
reported origin and outcome without repeating the action or inventing leftover
installation records. Missing files alone do not prove uninstallation. For
troubleshooting inspect the exact target; known-target management can proceed
directly. Acknowledge a settled notice from its result; do not query or reload
merely to confirm it. Diagnose reported damage using the exact public target
and relevant files, without auditing Runtime implementation as a routine step.

Distinguish a missing executable or unreachable service from missing config.
Valid config still uses UPDATE_LOCAL_MCP or REMOVE_LOCAL_MCP; confirmed absent
config can be recreated with ADD_LOCAL_MCP. Unparseable existing config may block
management: report the specific error; a parse failure does not prove that an
entry is absent or a previous save failed. Do not infer who changed the file or
how it became malformed. Do not propose clearing or rewriting managed YAML as
a repair shortcut; explain that malformed existing config needs explicit
configuration recovery outside this management path. Do not bypass the owner with raw YAML
writes or claim private credentials were cleaned. Diagnosis alone does not
authorize deleting or recreating a connection.


Saving, authorizing, discovering tools and executing a tool are distinct outcomes.
Read mutation status and `adoption` separately. `APPLIED` means the change settled;
it does not prove availability or whether a form appeared. Managed changes adopt
at the existing safe point before a later request without rewriting current
provider roots. For partial adoption, inspect the reported failures and retry
observation/adoption, not the completed mutation. Explicit `reload_capabilities`
is for partial adoption or out-of-band changes, not a routine second step.

List `kind:"MCP_TOOL"` with the copied MCP_SERVER target as `parent`, then inspect
an exact tool target. Its `input_schema` and `invocation.mode` decide the call:
`DIRECT` uses the named native tool; `META` uses the returned `tool_ref` with
`use_new_mcp_tool`; `UNAVAILABLE` explains the obstacle. Never guess schemas,
names or availability. Resource/prompt metadata is inspected before
`read_mcp_resource` / `get_mcp_prompt`.

Perform a representative call only when requested and appropriate; report what
passed and what remains unavailable. Authentication failures or disconnects do
not authorize replaying tool calls. A successful call does not establish
statelessness or read-only effects. Ordinary tool, process and network permissions
apply to both independent and Plugin connections.
