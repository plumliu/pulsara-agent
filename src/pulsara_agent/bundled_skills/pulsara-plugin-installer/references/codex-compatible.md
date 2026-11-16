# Codex Plugin declarations

Use `source_format:"codex"` for a distribution with `.codex-plugin/plugin.json`.
For a native Agent Plugins package, root `plugin.json` supplies its identity,
`skills/` its Skills and `mcp.json` its MCP services. OpenAI Hook settings may be
under `extensions.com.openai` in that root manifest.

## Hook source selection

For a native package:

1. `dev.pulsara/hooks/hooks.json`, when present, supplies Pulsara Hooks.
2. Otherwise an object at `extensions.com.openai` supplies OpenAI settings.
3. When that object is absent, `.codex-plugin/plugin.json` can supply those settings.

An omitted `hooks` in the selected settings uses `hooks/hooks.json` when present.
Explicit empty declarations stay empty. With no selected extension or overlay,
a plain `hooks/` directory remains a resource. A rejected selected Pulsara
source is represented by an empty installed configuration so its selection stays
in place.

A selected `hooks` can be a path, path array, inline object or object array.
Native OpenAI settings use their actual manifest `config_path` as the HOOK_SOURCE
Target. Read the manifest and referenced files for details. External Codex import
writes the selected definitions into the single Pulsara Hook source.

## Runtime behavior

Only Pulsara's documented eleven events and command handlers are active.
Configuration accepts `matcher:null` as omitted and `command_windows` as an alias
for `commandWindows`. Other events and handler types are listed as inactive.

Scripts receive Pulsara's native tool names and parameters. Matcher aliases only
select operations; a script using Codex-specific tool arguments may need a source
change when the task is to port that behavior. `transcript_path` is null.
PreToolUse can inspect and block a call; permission decisions and continuation
follow the [Hook configuration](pulsara-hook-extension.md).

Plugin commands receive `PLUGIN_ROOT` and `CLAUDE_PLUGIN_ROOT` for the package,
and `PLUGIN_DATA` and `CLAUDE_PLUGIN_DATA` for persistent Plugin data. Use the
command shell's variable syntax. Original scripts and resource layout are retained.

Apps and other OpenAI-specific UI features are described as inactive. Installation
remains separate from Plugin enablement and Hook trust.
