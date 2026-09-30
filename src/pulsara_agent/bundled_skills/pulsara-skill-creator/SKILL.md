---
name: pulsara-skill-creator
description: Create or improve portable local skills that follow the Agent Skills SKILL.md contract. Use when the user asks to design a new skill, review a skill bundle, or turn a repeated workflow into a local skill.
---

# Pulsara Skill Creator

Use this skill to create or improve a portable Agent Skills bundle for Pulsara.

## Workflow

1. Use the concrete workflow already supplied; ask only for missing information
   that changes the Skill's design.
2. Create a source directory named with lowercase letters, digits, and hyphens.
   Authoring a source is separate from installation; do not write into a managed
   installation directory to bypass the installer.
3. Write `SKILL.md` with YAML frontmatter:

```yaml
---
name: example-skill
description: A concise sentence that explains what the skill does and when to use it.
---
```

   Optional portable fields are `license`, `compatibility`, and bounded string-to-string `metadata`. Do not add them unless they carry real information.
4. Put long guidance in the body, not in frontmatter. When editing an existing skill, preserve valid portable fields and resources that still have consumers.
5. Create `references/`, `scripts/`, or `assets/` only when the workflow genuinely needs them.
6. Keep the skill progressive: the root `SKILL.md` should route to deeper files instead of inlining every detail.
7. For authoring-only validation, use the installed Pulsara launcher:

```bash
pulsara skills validate <skill-directory>
```

This command validates source files only; it does not install or adopt them.
Report the real result. If `pulsara` is not on `PATH`, report standalone validation
as unavailable; do not use a repository `.venv`, `uv run`, or a private validator.
For an already authorized installation, use the running Host's
`INSTALL_LOOSE_SKILL`, which performs native validation; a separate preliminary
CLI validation is unnecessary. Follow `pulsara-skill-installer` for that flow;
do not describe authoring or a passed validation as installation.

## Guardrails

- A Skill is portable, untrusted guidance; it does not grant tools, permissions, Hooks, MCP access, or execution authority.
- Do not add `agents/openai.yaml`, tool schemas, permission declarations, Hooks, MCP configuration, Pulsara metadata, or dependency fields to a loose skill.
- Keep MCP, Hook, credential, and permission configuration outside a loose Skill instead of encoding installation authority in frontmatter.
- Do not invent a `.system` root or graph entry for the skill.
- Prefer ordinary files that `read_file` and `terminal` can inspect naturally.
