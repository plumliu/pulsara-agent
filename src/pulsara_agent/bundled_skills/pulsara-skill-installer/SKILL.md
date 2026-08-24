---
name: pulsara-skill-installer
description: Install and inspect portable loose Agent Skills through Pulsara's official local management commands. Use when the user asks to install a local skill, choose workspace or user scope, list effective skills, or diagnose why a local skill is not effective.
---

# Pulsara Skill Installer

Use this skill to install a complete local Skill directory through Pulsara's official loose-Skill management service and global CLI.

## Workflow

1. Identify the source Skill directory and ask which scope the user wants if it is not already explicit:
   - `workspace` publishes under `<workspace>/.pulsara/skills`;
   - `user` publishes under `${PULSARA_HOME}/skills` and does not depend on the current workspace.
2. For workspace scope, identify the intended workspace root. A relative source path is still relative to the terminal's current directory.
3. Install with the global launcher:

```bash
pulsara skills install --scope workspace --workspace <workspace-root> <source-skill-directory>
```

or:

```bash
pulsara skills install --scope user <source-skill-directory>
```

4. Confirm the effective winner with:

```bash
pulsara skills list --workspace <workspace-root>
```

5. If the Skill is invalid, shadowed, absent, or the catalog is unavailable, inspect the same catalog truth with:

```bash
pulsara skills doctor --workspace <workspace-root>
```

6. Report the physical destination and explain that filesystem changes are discovered at the next legal provider safe point; they do not retroactively change an already-open model request.

Read `references/directory-contract.md` when the user needs the filesystem and race semantics explained.

## Guardrails

- Do not choose workspace or user scope on the user's behalf when their intent is ambiguous.
- Do not overwrite, merge, update, remove, roll back, or force an existing destination.
- Do not use raw `cp`, `copytree`, or a private script to imitate official installation.
- Loose Skill installation does not create bundled or Plugin provenance.
- Ordinary terminal and permission ownership still applies to every CLI invocation.
- If `pulsara` is not on `PATH`, report a Pulsara distribution or launcher setup problem. Do not search for a source checkout, use `.venv/bin/pulsara`, `python -m`, or `uv run`.
