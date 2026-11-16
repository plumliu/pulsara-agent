---
name: pulsara-mcp-installer
description: Set up MCP connections, configure authentication, discover their tools and resources, and diagnose connection or calling failures.
---

# Pulsara MCP Connections

## Discover and use

Use `query_capabilities` with `kind:"MCP"`:

| Request | Result |
| --- | --- |
| No target | Service records with status and available instructions/resource counts |
| `query:"keywords"` | Matching tool interfaces followed by matching services |
| An MCP_CONNECTION Target | That service's tool directory |
| An MCP_TOOL Target | The tool's complete interface and invocation reference |

Lists can use `scope:"USER"|"WORKSPACE"` and a PLUGIN `parent`. Exact queries
use `kind` and `target`. Search tool hits already contain their interface and
invocation reference; service hits can be expanded with the connection Target.
While some services are still being discovered, search can return ERROR with
usable tool interfaces; read the content for which services remain unknown.

Follow the service instructions and tool `input_schema`. Call `call_mcp_tool`
with the returned `invocation.target` and matching `arguments`. Available
`output_schema` describes successful `structuredContent`; ordinary result text
and images are in ordered `content`. A reference is reusable throughout the same
session. For JS composition, `pulsara-code-mode` explains how to inspect results
before writing a business Cell.

Read failure content for whether the remote call was dispatched and what is
known about its effects. An invalid reference can be refreshed by querying the
tool; a temporary connection failure alone does not invalidate it. An uncertain
dispatched call needs its outcome checked before retrying.

`read_mcp_resource` with an MCP_CONNECTION lists resources and URI templates.
Read an actual resource with its returned MCP_RESOURCE Target. For a template,
fill the variables in `uri_template` and use that concrete URI with the template's
returned `server_id`. Native NEXT continues the saved result through read_file.

## Set up a connection

Connection management uses ROOT-native `manage_capability`. USER configuration
is `<effective Pulsara home>/mcp.yaml`, defaulting to `~/.pulsara/mcp.yaml`;
WORKSPACE configuration is `<GUI project>/.pulsara/mcp.yaml`. Quick-start sessions
use USER. Targets carry the actual absolute configuration path and service key.

Get the service's documented MCP endpoint or stdio command and arguments.
Choose `streamable_http`, explicit legacy `sse`, or `stdio` accordingly.
For a new independent connection:

```json
{"target":{"kind":"MCP_CONNECTION","config_path":"/actual/project/.pulsara/mcp.yaml","server_id":"docs"},"action":"ADD_LOCAL_MCP"}
```

Omitting `config` opens the connection editor. Existing independent and Plugin
connections use `UPDATE_MCP_CONNECTION` with their observed Target; omission opens
a prefilled editor. A supplied config object updates the fields it contains.

Use the editor for credentials and advanced settings. A connection configured for
OAuth uses `AUTHORIZE_MCP` with the same Target; the user completes the login.
`CLEAR_MCP_AUTHORIZATION` clears the local OAuth authorization. Independent
connections can be enabled/disabled through UPDATE's config; Plugin connections
follow their Plugin's enabled setting. New connections default to ROOT and
subagent use, which users can restrict on the capability page.

Read [configuration details](references/connection-configuration.md) when supplying
public config, changing transports or working with Plugin connection settings.

## Verify and recover

Saving, authentication and background discovery have separate results. Query the
service status and the required tool to check readiness. After refresh, existing
sessions can discover and call ready connections. Use a representative call when
it is part of the task, choosing its parameters for the intended effect.

Use the returned Target for subsequent actions. The configured service key can
differ from the `server_id` in tool and resource Targets. A missing executable or
unreachable service can be repaired on the existing connection with
`UPDATE_MCP_CONNECTION`; removal uses `REMOVE_LOCAL_MCP` for independent connections.
A confirmed missing entry can be added again. Malformed configuration requires
recovering valid configuration at the reported path before normal management
continues.

Read `meta.status` and `content`: ERROR can include a saved change followed by a
refresh or cleanup failure. Resume from the actual completed steps. Normal
management refreshes capabilities; use `reload_capabilities` for a reported
refresh failure or changes made outside the app.
