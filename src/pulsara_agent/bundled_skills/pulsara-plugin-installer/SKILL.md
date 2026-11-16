---
name: pulsara-plugin-installer
description: Install, customize or replace native/Claude/Codex/Cursor Plugins, configure their components, and diagnose imports or Hooks. Use also for authoring local Hooks; known-target queries, toggles, removal and trust actions use the capability tools directly.
---

# Pulsara Plugin Installer

## Install and activate

Obtain the exact local source directory and identify its selected distribution
format. Use ROOT-native `manage_capability`; reuse the user's scope (WORKSPACE
means the GUI project), asking only when ambiguous. For a Claude distribution:

```json
{"target":{"kind":"PATH","path":"/absolute/source/plugin"},"action":"INSTALL_PLUGIN","scope":"WORKSPACE","source_format":"claude"}
```

Use the actual format: `native`, `claude`, `codex` or `cursor`. The official importer
owns conversion to Agent Plugins 1.0 and native validation. Read the selected
manifest to identify its format/components, preserve licenses and do not execute
downloaded code. Never union neighboring manifests, audit every script as an
installation prerequisite, or manually convert a supported distribution.
Unsupported host features are reported and not activated; supported components
must pass validation. Installation does not promise the original host behavior.

1. Install with `INSTALL_PLUGIN`. Set `replace:true` only for user-authorized
   replacement. Importer errors about paths, source races or secrets cannot be
   bypassed. Parameterized sources needing user input use the GUI import wizard;
   details are in `references/conversion-contract.md`.
2. Installation and replacement both produce a disabled instance. Reuse the
   result's confirmed PLUGIN `target` for inspection and management; plugin_path
   identifies the stable instance, not a package revision.
3. Configure existing package MCP connections with
   `UPDATE_MCP_CONNECTION` and their `MCP_CONNECTION` target;
   omit `config` for the shared editor. Users enter credentials privately there.
   A public config patches explicit fields; config:null restores package/default
   settings by removing the entire overlay.
   Configured OAuth connections use `AUTHORIZE_MCP` with the same target.
4. Enable the whole instance with `SET_PLUGIN_ENABLED` and `enabled:true`:

```json
{"target":{"kind":"PLUGIN","plugin_path":"/actual/plugin/instance"},"action":"SET_PLUGIN_ENABLED","enabled":true}
```

Only user submission accepts the current component/credential-destination review.
Do not assert acceptance or treat missing connection inputs as failed installation.
Plugin enablement and Hook trust are separate; a Hook source may also be disabled.
Individual Skill/MCP declarations are managed through their Plugin owner.

## Customize an installed Plugin

Inspect the exact Plugin target and edit its source directory with file tools or
terminal. If the original source is unavailable, make a writable source copy of
the actual observed installed package and use `source_format:"native"`; do not
guess its revision directory or treat plugin_path as the package root. Keep the
manifest name and installation scope to replace the same instance. Modify the requested Skills,
scripts or other components there, then use `INSTALL_PLUGIN` with `replace:true`
and follow the enablement flow above. The importer owns conversion and packaging;
installed package files remain read-only. Editing the source alone does not update
the installed instance.

## Verify or diagnose

A capability-page result notice can be acknowledged without repeating its action
or querying merely to confirm it. For troubleshooting use the exact Target and
relevant files, not a routine Runtime implementation audit.

An unreadable package does not mean its instance was uninstalled. If the exact
instance identity remains observable, requested removal uses REMOVE_PLUGIN.
For a damaged package, check an available replacement source, then remove and
install normally; INSTALL performs full validation. The two steps are not
atomic. If instance identity is unavailable, report that limit rather than editing
managed state or guessing credential ownership. Diagnosis alone does not
authorize removal or reinstallation.

List packages with `query_capabilities({kind:"PLUGIN"})`; an exact PLUGIN query
returns its status and component Target items. To list its Skills, Hooks or MCP
services, use its Target as parent with kind SKILL, HOOK or MCP. Exact component
queries use only target and kind. Follow `pulsara-mcp-installer` to observe actual
tool interfaces and call them through their shared MCP_TOOL_REF.

Read `meta.status` and actual content: ERROR can retain a confirmed change plus
cleanup/refresh failure. Address that failure without replaying installation.
Use ROOT-native reload_capabilities for incomplete adoption or out-of-band changes,
not as a routine second step. Perform a representative call only when requested.

Requested removal uses `REMOVE_PLUGIN` for the exact instance: dedicated
credentials are removed, data and the original source remain. Existing consumers
may finish using the old package before garbage collection.

## Hook workflows and references

For Hook diagnostics query kind HOOK with an observed HOOK_SOURCE Target
containing its absolute config_path. `TRUST_HOOK_SOURCE`
requires full current GUI review even under full access; enabling a Hook does not
grant trust or enable its Plugin. Hook management actions control trust and
enablement, not definition editing.

If trust state was unreadable, a native mutation may recreate it with defaults.
Post-mutation enablement does not prove the prior state or another actor's actions.

For user-requested **local** Hook authoring, use the resolved `config_path` with
file tools. Use `manage_capability` for user-requested trust or enablement; editing
definitions alone does not authorize activation. Installed package Hook definitions remain
immutable. Read `references/hook-source-management.md` for authoring and
source-control examples; it is sufficient for ordinary local Hook work.

Read other references only for the corresponding issue:

- `references/conversion-contract.md`: import diagnostics, GUI import and advanced CLI administration.
- `references/codex-compatible.md`: Codex-specific incompatibilities.
- `references/pulsara-hook-extension.md`: detailed Hook conversion, inputs and control semantics.

Do not bypass managed owners or Hook review with private installers, direct state
edits or terminal `--yes`.
