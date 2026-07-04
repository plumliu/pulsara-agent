# Pulsara Local Skill Directory Contract

Installed workspace skills live at:

```text
<workspace>/.pulsara/skills/<skill-name>/SKILL.md
```

A user-provided skill can start somewhere else, such as the repository root. It is not considered installed until its full directory has been copied under `.pulsara/skills`.

## Skill Folder Rules

- The folder name must match the `name` in `SKILL.md`.
- `SKILL.md` must begin with YAML frontmatter.
- The frontmatter must include string fields named `name` and `description`.
- Skill names use lowercase letters, digits, and hyphens only.
- `scripts/`, `references/`, and `assets/` are copied as part of the skill.

The installer refuses overwrites so installation remains easy to reason about.
