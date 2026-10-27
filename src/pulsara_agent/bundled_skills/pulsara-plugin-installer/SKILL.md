---
name: pulsara-plugin-installer
description: Install, customize or replace native/Claude/Codex/Cursor Plugins, configure their components, and diagnose imports or Hooks. Use also for authoring local Hooks; known-target queries, toggles, removal and trust actions use the capability tools directly.
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
installation prerequisite, or manually convert a supported distribution.
Unsupported host features are reported and not activated; supported components
must pass validation. Installation does not promise the original host behavior.

1. Install with `INSTALL_PLUGIN`. Set `replace:true` only for user-authorized
   replacement. Importer errors about paths, source races or secrets cannot be
   bypassed. Parameterized sources needing user input use the GUI import wizard;
   details are in `references/conversion-contract.md`.
2. Installation and replacement both produce a disabled instance. Reuse the
   result's `target` for inspection and management. `current.package_root` is
   the installed package; never derive its ID from a path.
3. Configure existing package MCP connections with
   `CONFIGURE_PLUGIN_MCP_CONNECTION` and their `MCP_CONNECTION` target;
   omit `overlay` for the shared editor. Users enter credentials privately there.
   Configured OAuth connections use `AUTHORIZE_MCP` with the same target.
4. Enable the whole instance with `SET_PLUGIN_ENABLED` and `enabled:true`:

```json
{"action":"SET_PLUGIN_ENABLED","target":{"kind":"PLUGIN","scope":"WORKSPACE","plugin_id":"example"},"enabled":true}
```

Only user submission accepts the current component/credential-destination review.
Do not assert acceptance or treat missing connection inputs as failed installation.
Plugin enablement and Hook trust are separate; a Hook source may also be disabled.
Individual Skill/MCP declarations are managed through their Plugin owner.

## Customize an installed Plugin

Inspect the exact Plugin target and edit its source directory with file tools or
terminal. If the original source is unavailable, make a writable source copy of
`current.package_root` and use `source_format:"native"`. Keep the manifest name
and installation scope to replace the same instance. Modify the requested Skills,
scripts or other components there, then use `INSTALL_PLUGIN` with `replace:true`
and follow the enablement flow above. The importer owns conversion and packaging;
installed package files remain read-only. Editing the source alone does not update
the installed instance.

## Verify or diagnose

Capability-page actions may produce a brief Runtime result notice. Use its
reported origin and outcome without repeating the action or inventing leftover
installation records. Missing files alone do not prove uninstallation. For
troubleshooting inspect the exact target; known-target management can proceed
directly. Acknowledge a settled notice from its result; do not query or reload
merely to confirm it. Diagnose reported damage using the exact public target
and relevant files, without auditing Runtime implementation as a routine step.

An unreadable package does not mean its instance was uninstalled. If the exact
instance identity remains observable, requested removal uses REMOVE_PLUGIN.
For a damaged package, check an available replacement source, then remove and
install normally; INSTALL performs full validation. The two steps are not
atomic. If instance identity is unavailable, report that limit rather than editing
managed state or guessing credential ownership. Diagnosis alone does not
authorize removal or reinstallation.

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
