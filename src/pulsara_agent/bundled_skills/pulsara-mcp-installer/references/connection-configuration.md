# MCP connection settings

## Public config

A new connection requires its transport type and the branch's required fields.
For a known HTTP endpoint with no authentication:

```json
{"transport":{"type":"streamable_http","endpoint":"https://example.org/mcp"},"auth":{"type":"none"}}
```

| Field | Use |
| --- | --- |
| `enabled` | Independent connection's enabled setting; ADD defaults to true |
| `transport.type` | `stdio`, `streamable_http` or `sse` |
| `transport.command` | Required for stdio |
| `transport.args` | Ordered stdio arguments, default `[]` |
| `transport.env` | Public environment values, default `{}` |
| `transport.cwd` | Workspace-relative stdio directory, default workspace root |
| `transport.endpoint` | Required HTTP/SSE endpoint, including its actual path |
| `public_headers` | Public HTTP/SSE headers |
| `auth.type` | `none` (default), `oauth`, `bearer` or `static_headers` |

Credentials and advanced choices are collected in the connection editor. Omitting
`config` opens that editor for ADD or UPDATE. The capability page can also import
user-selected JSON/JSONC, with a preview, field classification and input fields.

UPDATE patches provided fields, including individual transport fields. `env` and
`public_headers` replace their whole editable maps: `{}` clears a map, omission
preserves it. `args:[]` clears the argument list. Keeping the same auth type
preserves its private settings. Switching an independent transport requires the
new branch's required command or endpoint.

## Plugin connections

A Plugin update uses the same `UPDATE_MCP_CONNECTION` action. Its package supplies
the transport type, command, arguments and working directory. Public updates can
change the applicable endpoint, environment, headers and authentication settings.
Enablement follows the Plugin.

`config:{}` preserves the existing settings. Plugin `config:null` removes the
entire connection override, restoring package/default settings and the default
ROOT-and-subagent scope. Object updates preserve omitted settings, including a
user's saved scope. Use the editor to supply private values or advanced policy.

## Availability and administration

The configured connection key belongs in MCP_CONNECTION management Targets.
Tool and resource discovery supplies their runtime service identifier separately;
copy the Target from the result used for that operation.

A saved configuration can still need login, a local executable or a successful
handshake. Read the reported step and reason when diagnosing availability.
`CLEAR_MCP_AUTHORIZATION` clears local authorization; it does not promise remote
account revocation. `REMOVE_LOCAL_MCP` removes an independent connection and its
associated local credentials/authorization. Plugin connections are removed with
their Plugin.

For requested standalone administration, inspect `pulsara mcp --help`. Refresh
out-of-app changes from the capability page or `reload_capabilities` afterward.
