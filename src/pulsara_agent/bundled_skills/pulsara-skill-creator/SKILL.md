---
name: pulsara-skill-creator
description: Create or improve portable local skills that follow the Agent Skills SKILL.md contract. Use when the user asks to design a new skill, review a skill bundle, or turn a repeated workflow into a local skill.
---

# Pulsara Skill Creator

Use this skill to create or improve a portable Agent Skills bundle for Pulsara.

## Workflow

1. Clarify the concrete workflow the skill should help with.
2. Create a skill directory named with lowercase letters, digits, and hyphens.
3. Write `SKILL.md` with YAML frontmatter:

```yaml
---
name: example-skill
description: A concise sentence that explains when to use the skill.
---
```

4. Put long guidance in the body, not in frontmatter.
5. Use `references/` for detailed instructions, `scripts/` for repeatable commands, and `assets/` for reusable templates.
6. Keep the skill progressive: the root `SKILL.md` should route to deeper files instead of inlining every detail.

## Guardrails

- Do not add tool schemas, permission declarations, or Pulsara dependency fields to a skill. Describe how to use ordinary available tools in the Markdown body.
- Do not invent a `.system` root or graph entry for the skill.
- Prefer ordinary files that `read_file` and `terminal` can inspect naturally.
