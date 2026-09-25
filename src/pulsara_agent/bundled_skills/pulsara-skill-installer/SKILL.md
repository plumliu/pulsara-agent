---
name: pulsara-skill-installer
description: Install and inspect portable loose Agent Skills through Pulsara's official local management commands. Use when the user asks to install a local skill, choose workspace or user scope, list effective skills, or diagnose why a local skill is not effective.
---

# Pulsara Skill Installer

Use this skill to install a complete local Skill directory through Pulsara's official loose-Skill management service and global CLI.

The capability page also provides a model-free import workflow: select a local
directory (including `.opencode/skills`, `.claude/skills` or `.agents/skills`),
preview candidates, supply a missing description and install selected Skills.
It preserves the original source and all references/scripts/assets; metadata
normalization happens only in the native installation candidate. It does not
execute JS/TS host plugins found alongside independent Skills.

User/workspace loose installations can be removed through the capability page.
Only an explicitly selected
managed copy is removed; bundled definitions and Plugin children are not loose
deletion targets. A same-name lower-priority Skill may become effective again.

## Workflow

1. Identify the source Skill directory and ask which scope the user wants if it is not already explicit:
   - `workspace` publishes under `<workspace>/.pulsara/skills`;
   - `user` publishes under `${PULSARA_HOME}/skills` and does not depend on the current workspace.
2. For workspace scope, identify the intended workspace root. A relative source path is relative to this command's working directory: the workspace root unless this call supplies `workdir` or changes directory inside the command. Earlier terminal calls do not change it.
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

   Pulsara's own bundled Skills are read-only package defaults. A same-name
   workspace or user Skill is an ordinary higher-priority override; deleting
   that loose directory makes the bundled definition eligible again at the
   next complete safe point.

5. If the Skill is invalid, shadowed, absent, or the catalog is unavailable, inspect the same catalog truth with:

```bash
pulsara skills doctor --workspace <workspace-root>
```

6. Report the installation directory. Updated Skills can be discovered when Pulsara next prepares a model request; an already-running request is unchanged.

Read `references/directory-contract.md` when the user needs the filesystem and race semantics explained.

## Guardrails

- Do not choose workspace or user scope on the user's behalf when their intent is ambiguous.
- Do not overwrite, merge, roll back or force an existing destination. Use the
  official removal operation only when the user requests that exact deletion.
- Do not use raw `cp`, `copytree`, or a private script to imitate official installation.
- Loose Skill installation does not create a receipt, ownership marker, or managed provenance.
- Ordinary terminal and permission ownership still applies to every CLI invocation.
- If `pulsara` is not on `PATH`, report a Pulsara distribution or launcher setup problem. Do not search for a source checkout, use `.venv/bin/pulsara`, `python -m`, or `uv run`.
