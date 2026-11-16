---
name: pulsara-plugin-installer
description: Install, configure, customize or replace Plugins from native, Claude, Codex or Cursor distributions, and create or troubleshoot local Hooks.
---

# Pulsara Plugin Installer

## Install and activate

Prepare the Plugin's local source directory and choose its distribution:
`native`, `claude`, `codex` or `cursor`. Read the corresponding manifest to identify
the format and declared components. The importer handles conversion and keeps
independently usable components, reporting anything left inactive.

Use ROOT-native `manage_capability`. USER installs for the current user;
WORKSPACE installs for the current GUI project. Quick-start sessions use USER.
For a Claude distribution:

```json
{"target":{"kind":"PATH","path":"/absolute/source/plugin"},"action":"INSTALL_PLUGIN","scope":"WORKSPACE","source_format":"claude"}
```

Installation returns a PLUGIN Target and leaves the instance disabled. A successful
result can include notices about unsupported components; use those notices to
explain which requested features are available.

Configure package MCP connections with `UPDATE_MCP_CONNECTION` and their returned
MCP_CONNECTION Targets. Omit `config` to open the editor for connection parameters
and credentials. Configured OAuth connections use `AUTHORIZE_MCP`.

Enable the instance with:

```json
{"target":{"kind":"PLUGIN","plugin_path":"/actual/plugin/instance"},"action":"SET_PLUGIN_ENABLED","enabled":true}
```

The user completes the component and connection review. Plugin enablement and
Hook trust are separate: a Hook runs when its Plugin and source are enabled and
the source is trusted. Use `TRUST_HOOK_SOURCE` for the Hook review.

After refresh, enabled and trusted Hooks apply to future events in existing
sessions; enabling or trusting does not replay `SessionStart`.

## Inspect and customize

List Plugins with `query_capabilities({kind:"PLUGIN"})`. An exact PLUGIN query
returns its status and component Targets. Use a PLUGIN Target as `parent` with
kind SKILL, HOOK or MCP to list its components; exact component queries use only
`kind` and `target`. MCP tools and resources are described in
`pulsara-mcp-installer`.

To customize a Plugin, edit its source, then install in the same scope with
`replace:true`. Keep the manifest name when replacing the same instance.
Replacement also leaves it disabled, ready for review and enablement.

If the source is unavailable, locate the installed package from its returned component
paths and use a writable copy as a native source. A PLUGIN Target's
`plugin_path` identifies the installation instance rather than the source or a
particular package directory. Source edits take effect after replacement.

## Read outcomes and recover

Use `meta.status` and `content` for actual effects. ERROR may include a completed
installation followed by cleanup or refresh failure; continue from the failed
step. Normal management refreshes capabilities automatically; `reload_capabilities`
is for a reported refresh problem or changes made outside the app.

For a damaged package, use its confirmed instance Target for removal. Prepare a
replacement source before a requested remove-and-install recovery. Removal
preserves the original source and Plugin data, while cleaning its associated
local credentials and authorization.

Local Hook files can be edited with file tools. Plugin Hook changes go through
source editing and package replacement. Query kind HOOK for the actual source
Target; its `config_path` can be a manifest referring to several Hook files.

Read the reference matching the task:

- [Import details](references/conversion-contract.md): formats, resources and partial imports.
- [Codex declarations](references/codex-compatible.md): OpenAI settings and source selection.
- [Local Hook workflows](references/hook-source-management.md): create, edit, trust and toggle.
- [Hook configuration](references/pulsara-hook-extension.md): events, command inputs and outputs.
