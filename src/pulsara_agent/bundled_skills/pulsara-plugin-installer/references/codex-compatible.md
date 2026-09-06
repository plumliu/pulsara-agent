# Codex-Compatible Package Conversion

Select Codex explicitly when importing `.codex-plugin/plugin.json`. Marker
presence is evidence to inspect, not proof that conversion will succeed; the
official importer creates the native candidate without model rewriting.

Absent declarations use only the selected format's defaults. Explicit empty
MCP/hooks declarations must not load neighboring host files. Skill resources
remain complete. Private inputs are configured on the disabled installed instance,
not copied into the native candidate. An import never grants enablement acceptance.

## Inspect

Read the exact Codex manifest and every referenced local declaration. Also inspect
default `skills/`, `.mcp.json`, `.app.json`, Hook configuration, package-relative
launchers, relevant Skill instructions, and referenced scripts. Determine which
parts are merely interface metadata and which parts are necessary to the Plugin's
stated behavior.

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
  `pulsara-hook-extension.md`. Read that target reference before mapping any Hook.
- Keep interface and branding fields as inert source resources unless Agent
  Plugins 1.0 has an exact portable metadata destination. They grant no capability.

## Blockers

Exact conversion is not possible when the Plugin depends on a Codex-private App,
host-injected tool, desktop bridge, private launcher contract, undeclared runtime,
unsupported MCP setting, credential acquisition flow, or UI behavior that Pulsara
does not provide. Fields such as App declarations, host environment forwarding,
private timeout/restart policy, or bundled-content variants must not be silently
dropped when they affect behavior.

A Codex Hook that requires a real transcript path, Codex-owned App/tool input,
transparent `write_stdin` transport, result or argument mutation, output
suppression, or a handler other than the exact supported command subset is also a
blocker. Do not infer target behavior from the shared event name alone.

For example, a package whose purpose is controlling a Codex-owned desktop bridge
does not become equivalent merely because its Skill text parses. Report the host
dependency and refuse whole-Plugin conversion.
