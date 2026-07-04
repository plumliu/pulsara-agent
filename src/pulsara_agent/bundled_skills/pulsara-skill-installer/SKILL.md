---
name: pulsara-skill-installer
description: Install a local OpenAI/Codex-style skill folder into this workspace's Pulsara skill directory. Use when the user says they placed a SKILL in the repository root, asks to install a local skill, asks to list installed Pulsara skills, or asks to verify local skill installation.
provides_tools:
  - terminal
---

# Pulsara Skill Installer

Use this skill to install a local skill directory into the current workspace's `.pulsara/skills` directory.

## Workflow

1. Identify the workspace root and the source skill directory.
   - If the user says the skill is in the repository root, resolve the named folder relative to the workspace root.
   - Do not treat an already installed skill under `.pulsara/skills` as a source unless the user explicitly asks to inspect it.
2. Read `references/directory-contract.md` before installing so the destination and validation rules are clear.
3. Install with the helper script:

```bash
python <this-skill-directory>/scripts/install-local-skill.py --workspace <workspace-root> --src <source-skill-directory>
```

4. If the script succeeds, list installed skills:

```bash
python <this-skill-directory>/scripts/list-installed-skills.py --workspace <workspace-root>
```

5. Tell the user the installed destination and that a new Pulsara turn or `pulsara host inspect` should discover the skill.

## Guardrails

- Refuse to overwrite an existing installed skill unless a future script explicitly supports a force option and the user asks for it.
- Keep the whole skill directory intact when installing, including `scripts/`, `references/`, and `assets/`.
- If validation fails, report the script's exact error and do not manually copy partial files.
