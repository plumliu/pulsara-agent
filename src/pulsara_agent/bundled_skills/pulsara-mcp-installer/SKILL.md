---
name: pulsara-mcp-installer
description: Configure and verify local MCP servers through Pulsara's official management commands and running-Host reload/catalog tools. Use when the user asks to install, add, connect, configure, test, enable, disable, remove, or diagnose an MCP server in Pulsara.
---

# Pulsara MCP Installer

Use this skill to add an MCP server to Pulsara's existing local configuration
surface and prove that Pulsara can discover its tools. An MCP vendor's ordinary
CLI is not automatically its MCP server; use an authoritative Streamable HTTP
endpoint or stdio command intended for MCP.

## Product Availability

Use the installed global `pulsara` launcher. First confirm that the installed
distribution exposes `pulsara mcp`. If it does not, report a Pulsara distribution
or launcher blocker and stop. Do not fall back to a source checkout, repository
`.venv`, `python -m`, `uv run`, a private script, or direct YAML editing.

## Workflow

1. Establish the authoritative connection shape before writing configuration:
   - remote MCP: exact Streamable HTTP endpoint;
   - local MCP: exact executable plus each argument in order.
   Do not guess an endpoint, command, argument, credential, or transport from a
   product name alone.
2. Choose configuration visibility from the user's request:
   - user configuration applies across workspaces and omits `--workspace`;
   - workspace configuration uses `--workspace <workspace-root>` and applies only
     when that workspace MCP source is trusted by the Host.
   Ask only when this choice would materially change the result and the request
   does not already say global, user, machine, project, directory, or workspace.
3. Inspect existing truth before adding a duplicate:

```bash
pulsara mcp list
pulsara mcp list --workspace <workspace-root>
```

4. Add one remote server:

```bash
pulsara mcp add <server-id> --url <streamable-http-endpoint>
pulsara mcp add <server-id> --url <streamable-http-endpoint> --workspace <workspace-root>
```

   Or add one stdio server, keeping every argument separate and ordered:

```bash
pulsara mcp add <server-id> --stdio-command <executable> --arg <argument> --arg <argument>
```

5. Keep defaults unless evidence or the user's request requires otherwise:
   - use `--allow-http-localhost` only for an actual localhost HTTP endpoint;
   - use `--allow-private-network` only for an intended private-network endpoint;
   - use `--proved-stateless` only when the server contract proves stateless HTTP;
   - use `--required` only when Host startup must treat this server as required;
   - use `--scope ROOT_AND_SUBAGENTS` only when delegated tasks should see it;
   - keep `--effect AUTO` unless an authoritative contract proves a narrower
     `READ_ONLY` default or requires `EXTERNAL_EFFECT`.
6. Verify the exact configured server through Pulsara's production connector:

```bash
pulsara mcp doctor <server-id>
pulsara mcp doctor <server-id> --workspace <workspace-root>
```

   Do not claim success unless doctor reaches a ready state and reports the
   expected catalog. A reachable endpoint alone is not an installed MCP server.
7. In an already-running Host, call `reload_capabilities` after configuration changes.
   This existing reload path refreshes local MCP and Plugin sources without
   rewriting the installed provider-input prefix. Then call
   `list_mcp_servers` with the exact server id.
8. A newly discovered tool may not become a direct provider tool inside the
   current epoch. If its route is `NEW_MCP_META_ONLY`, pass its exact listed name
   to `inspect_new_mcp_tool`, read the returned schema, and invoke it only with
   `use_new_mcp_tool` and the exact returned tool reference. Never guess a tool
   name, schema, or tool reference. A new conversation may expose the tool
   directly at its cold-epoch boundary.
9. When verification is requested and a safe read-only MCP operation exists,
   invoke one representative operation within the user's stated scope. Report
   the connector state, discovered tool count, invoked tool, and concrete result
   or typed blocker.

Use the ordinary lifecycle commands for later management:

```bash
pulsara mcp enable <server-id> [--workspace <workspace-root>]
pulsara mcp disable <server-id> [--workspace <workspace-root>]
pulsara mcp remove <server-id> [--workspace <workspace-root>]
```

`pulsara mcp reconnect` requires an active Host-owned supervisor and cannot make
a standalone CLI process control another Host. Prefer the running Host's reload
and catalog path after a configuration change.

## Guardrails

- This skill grants no filesystem, terminal, network, remote-effect, workspace
  trust, or subagent authority. Ordinary permission policy still applies.
- Do not replace or remove an existing server id unless the user has requested
  that exact lifecycle change. There is no implicit update operation.
- Do not bypass Pulsara validation by editing `mcp.yaml` directly.
- Do not print, persist, or invent credentials. If the authoritative server
  requires an authentication shape the management surface cannot represent,
  report that exact blocker instead of silently connecting anonymously.
- Do not infer statelessness from a successful request or missing session header.
- Do not confuse direct endpoint protocol success with Pulsara connector success;
  use `mcp doctor` and the running catalog as the product evidence.
