# Codex-Compatible Package Conversion

Select Codex explicitly when importing `.codex-plugin/plugin.json`. Marker
presence is evidence to inspect, not proof that conversion will succeed; the
official importer creates the native candidate without model rewriting.

Absent declarations use only the selected format's defaults. Explicit empty
MCP/hooks declarations must not load neighboring host files. Skill resources
remain complete. Private inputs are configured on the disabled installed instance,
not copied into the native candidate. An import never grants enablement acceptance.

## Inspect

Read the exact Codex manifest to select the distribution. The official importer
observes its declarations, format defaults and complete admitted resources.
Inspect a referenced declaration, Skill or script when a concrete import
diagnostic or user question needs it; routine installation does not require
repeating the importer's complete source audit. Its supported field set decides
which components are presentation metadata and which require active behavior.

Do not infer semantics from the examples in this reference when the source says
something different.

## Conservative Mappings

- Map exact portable identity fields such as name, version, description, author,
  homepage, repository, license, and keywords into root `plugin.json` when they
  satisfy Agent Plugins 1.0.
- Copy valid Skill directories into the fixed root `skills/` placement without
  changing instructional bodies. Custom Skill paths may be relocated only when
  membership and names remain unambiguous and collision-free.
- Translate `.mcp.json` servers only when transport and every behavior-bearing
  field have exact Agent Plugins/Pulsara representations. Preserve command, args,
  cwd, environment data, endpoint, and public headers. Package-root path syntax may
  change only when it resolves to the same managed package location.
- Translate command Hooks only when their lifecycle event, matcher, handler, input,
  output, and control semantics exactly match
  `pulsara-hook-extension.md`. Use importer diagnostics; inspect that reference for a concrete compatibility question.
- Keep interface and branding fields as inert source resources unless Agent
  Plugins 1.0 has an exact portable metadata destination. They grant no capability.

## Unsupported host behavior

Codex-private Apps, desktop bridges, injected tools and UI behavior are reported
and not activated. Unsupported Hook events and handler types are skipped with
diagnostics. Valid supported command Hooks receive Pulsara native inputs; aliases
for matching do not translate script inputs or implement the original harness.
Invalid supported Skill/MCP/command Hook declarations still reject installation.

Plugin Hooks receive PLUGIN_ROOT / CLAUDE_PLUGIN_ROOT for the same managed package
and PLUGIN_DATA / CLAUDE_PLUGIN_DATA for the same persistent data directory. Braced
references in commands are resolved by the execution shell; scripts also receive
all four environment variables. MCP does not gain Claude-prefixed aliases.

Report any unsupported behavior relevant to the user's request. Successful
installation does not imply a Codex desktop bridge or other private behavior works.
