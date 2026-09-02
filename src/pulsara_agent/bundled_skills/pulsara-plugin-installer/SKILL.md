---
name: pulsara-plugin-installer
description: Validate and install local Agent Plugins 1.0 packages through Pulsara, and conservatively convert a local Codex-compatible package only when strict standard validation rejects its format. Use when the user asks to validate, install, adapt, enable, inspect, or diagnose a local plugin package.
---

# Pulsara Plugin Installer

Use this skill for local Plugin source directories. Pulsara Runtime and managed
installation accept only Agent Plugins 1.0 packages; this skill may author a new
standard candidate, but it cannot create another package identity or bypass the
production validator, publisher, trust, permission, or runtime owners.

## Product Availability

Use the installed global `pulsara` launcher. First confirm that the installed
distribution exposes `pulsara plugins`. If it does not, report that Pulsara Plugin
installation is not activated in that distribution and stop. Do not fall back to
a source checkout, repository `.venv`, `python -m`, `uv run`, raw filesystem copy,
or a private installer.

## Strict-First Workflow

1. Identify the local source directory. This workflow does not acquire remote
   repositories, archives, packages, dependencies, or credentials.
2. Identify `user` or `workspace` scope. Ask when scope is ambiguous. For
   workspace scope, identify the exact workspace root.
3. Always try the sole production validator before considering conversion:

```bash
pulsara plugins validate <source-plugin-directory>
```

4. If validation is `VALID`, do not rewrite the package. Continue with ordinary
   installation.
5. Do not attempt model conversion after a filesystem, symlink, special-file,
   secret, source-race, source-unavailable, cancellation, or deadline failure.
   Report the exact typed failure.
6. If deterministic diagnostics instead show a foreign package layout or a
   representable schema/component difference, read
   `references/conversion-contract.md` and `references/codex-compatible.md`.
   If the source contains behavior-bearing Hooks, also read
   `references/pulsara-hook-extension.md`.
7. Inspect the actual manifest, component declarations, Skill instructions,
   Hook/MCP entrypoints, referenced scripts, compatibility, and license. Never
   execute Plugin code while deciding whether conversion is possible.
8. Create a separate attempt-local candidate. Leave the source untouched and
   apply only the minimum transformations that preserve the Plugin's behavior.
9. Run the same production validator on the candidate. A model judgment is never
   a substitute for `VALID`. If a diagnostic identifies another representable
   difference, inspect the exact affected content and correct it narrowly. Stop
   once any required behavior has no exact Pulsara representation; do not loop,
   invent a substitute, or install a partial Plugin.
10. Before installation, report the files changed by conversion, every component
    mapping, any presentation-only data left inert, and all runtime dependencies.

Install a validated candidate with one of:

```bash
pulsara plugins add --scope workspace --workspace <workspace-root> <candidate-directory>
pulsara plugins add --scope user <candidate-directory>
```

Use `--replace` only when the user explicitly requests replacement of an existing
instance. Add and replace both leave the new package disabled.

Inspect the installed truth with `pulsara plugins list` and use
`pulsara plugins doctor` for component, availability, conflict, trust, or reload
diagnostics. Enabling is a separate decision: show the exact normalized component
summary and obtain the user's acceptance before invoking `pulsara plugins enable`.
`--yes` only records that exact external-process acceptance non-interactively; it
does not waive the summary, bind a different package install id, grant Hook trust,
or grant remote tool permission.

Use the ordinary lifecycle commands rather than editing managed state:

```bash
pulsara plugins disable --scope <user|workspace> [--workspace <workspace-root>] <plugin-id>
pulsara plugins remove --scope <user|workspace> [--workspace <workspace-root>] <plugin-id>
pulsara plugins gc [--workspace <workspace-root>]
```

Remove preserves the per-instance data directory. GC only reclaims unreferenced
package roots/stages that have no physical consumer. A running Host adopts
management changes only through `reload_capabilities` or restart;
install, replace, enable, disable, remove, trust, and GC never rewrite an already
installed provider prefix.

## Guardrails

- Preserve Plugin identity and intent. Do not silently rename it, rewrite its
  instructions, replace tools, weaken permissions, or drop active components.
- Preserve commands, arguments, working directories, URLs, public headers, and
  declared environment values exactly whenever they are representable.
- Treat Hook conversion as an exact target-contract join. A similar event name or
  executable path is not proof of equivalent owner timing, input, authority,
  control, or lifecycle behavior.
- Do not create or install agent definitions, worker profiles, presets, or child
  runtimes. `SubagentStart` and `SubagentStop` are existing Pulsara Hook
  lifecycle selectors only.
- Never guess or synthesize credentials, secret values, executable locations,
  network endpoints, dependencies, or compatibility claims.
- Do not run lifecycle scripts, dependency installers, MCP servers, Hooks,
  executables, or bundled binaries during conversion.
- Do not turn an unsupported Plugin into a smaller portable fork as part of an
  installation request. That requires a separate, explicit authoring request.
- If exact conversion is impossible, explain the blocking component and confirm
  that no Plugin was installed.
