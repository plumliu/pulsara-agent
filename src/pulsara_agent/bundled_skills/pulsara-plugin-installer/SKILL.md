---
name: pulsara-plugin-installer
description: Install or replace native/Claude/Codex/Cursor Plugins, configure their components for activation, and diagnose imports or Hook behavior. Use also for authoring local Hook definitions; known-target queries, toggles, removal and trust actions use the capability tools directly.
---

# Pulsara Plugin Installer

## Install and activate

Obtain the exact local source directory and identify its selected distribution
format. Reuse the user's scope; ask only when ambiguous. For a Claude distribution:

```json
{"action":"INSTALL_PLUGIN","scope":"WORKSPACE","source_path":"/absolute/source/plugin","source_format":"claude"}
```

Use the actual format: `native`, `claude`, `codex` or `cursor`. The official importer
owns conversion to Agent Plugins 1.0 and native validation. Read the selected
manifest to identify its format/components, preserve licenses and do not execute
downloaded code. Never union neighboring manifests, audit every script as an
installation prerequisite, or rewrite a supported distribution manually.
Unsupported host features are reported and not activated; supported components
must pass validation. Installation does not promise the original host behavior.

1. Install with `INSTALL_PLUGIN`. Set `replace:true` only for user-authorized
   replacement. Importer errors about paths, source races or secrets cannot be
   bypassed. Parameterized sources needing user input use the GUI import wizard;
   details are in `references/conversion-contract.md`.
2. Installation and replacement both produce a disabled instance. Reuse the
   result's `identity.plugin_id` and scope; never derive the ID from a path.
   `current.package_root` is the installed package. Inspect it with
   `target={kind:"PLUGIN",scope:<returned scope>,plugin_id:<returned id>}`.
3. Configure existing package MCP connections with
   `CONFIGURE_PLUGIN_MCP_CONNECTION`, `plugin_id` and their local `server_id`;
   omit `overlay` for the shared editor. Users enter credentials privately there.
   Configured OAuth connections use `AUTHORIZE_MCP` with both IDs.
4. Enable the whole instance with `SET_PLUGIN_ENABLED` and `enabled:true`:

```json
{"action":"SET_PLUGIN_ENABLED","scope":"WORKSPACE","plugin_id":"example","enabled":true}
```

Only user submission accepts the current component/credential-destination review.
Do not assert acceptance or treat missing connection inputs as failed installation.
Plugin enablement and Hook trust are separate; a Hook source may also be disabled.
Individual Skill/MCP declarations are managed through their Plugin owner.

## Verify or diagnose

For this package's capabilities, copy its target into `list_capabilities.parent`;
Plugin is a source, not a list kind. Complete package inventory stays in the GUI.
Read mutation and adoption separately: `APPLIED` means the source change settled,
not that all consumers adopted it or that a form appeared. First-party changes
adopt at existing safe points; current provider roots remain unchanged. Inspect
reported partial adoption without replaying installation. Explicit reload is for
partial adoption or out-of-band changes, not a routine second step. Perform a
representative call only when requested.

Requested removal uses `REMOVE_PLUGIN` for the exact instance: dedicated
credentials are removed, data and the original source remain. Existing consumers
may finish using the old package before garbage collection.

## Hook workflows and references

For Hook diagnostics inspect a listed `HOOK_SOURCE` target. `TRUST_HOOK_SOURCE`
requires full current GUI review even under full access; enabling a Hook does not
grant trust or enable its Plugin. Hook management actions control trust and
enablement, not definition editing.

For user-requested **local** Hook authoring, use the resolved `config_path` with
file tools. Use `manage_capability` for user-requested trust or enablement; editing
definitions alone does not authorize activation. Package Hook definitions remain
immutable. Read `references/hook-source-management.md` for authoring and
source-control examples; it is sufficient for ordinary local Hook work.

Read other references only for the corresponding issue:

- `references/conversion-contract.md`: import diagnostics, GUI import and advanced CLI administration.
- `references/codex-compatible.md`: Codex-specific incompatibilities.
- `references/pulsara-hook-extension.md`: detailed Hook conversion, inputs and control semantics.

Use terminal/file tools to prepare sources and the typed tool to install,
configure or authorize them. Do not bypass managed owners or Hook review with
private installers, direct state edits or terminal `--yes`.
