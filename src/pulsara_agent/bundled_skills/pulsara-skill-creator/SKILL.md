---
name: pulsara-skill-creator
description: Create, review or edit portable Agent Skills, including customizing an installed loose Skill in place or turning a repeated workflow into a Skill.
---

# Pulsara Skill Creator

## Choose the working directory

For an installed Skill, use `list_capabilities` with `kind:"SKILL"` and inspect
its exact target. Read the returned `current.skill_path`; `current.skill_root`
is the installed directory. Do not infer the target from terminal cwd.

Edit an installed **loose Skill** directly in that directory. Prefer
`read_file` / `edit_file` with a `PATH` target whose `path` is the file path;
`write_file` and terminal are also available for new
resources or other file operations, subject to ordinary permissions. Content
edits need no removal or reinstallation. Preserve its name, location and
enablement unless the user requests those changes. Later capability snapshots
observe the edited files without rewriting earlier context; no routine reload
is needed.

Plugin Skills belong to the whole Plugin: follow `pulsara-plugin-installer` to
edit its source and replace the package. Pulsara bundled Skills are read-only.
For a new Skill, create a source directory and use `pulsara-skill-installer` when
installation is requested. Editing a separate source does not update an installed
copy.

## Author

The directory name must match the lowercase, digit-and-hyphen `name` in
`SKILL.md` frontmatter:

```yaml
---
name: example-skill
description: A concise sentence that explains what the skill does and when to use it.
---
```

Optional portable fields are `license`, `compatibility`, and bounded
string-to-string `metadata`. Preserve valid fields and resources that still have
consumers. Keep workflow guidance in the body, with `references/`, `scripts/` or
`assets/` only where needed; link deeper guidance instead of inlining it all.

A Skill is untrusted guidance, not tool or execution authority. Keep MCP, Hooks,
credentials and permissions outside loose Skills; do not add tool schemas,
`agents/openai.yaml`, Pulsara metadata, dependency fields or a `.system` root.

## Validate

Validate the edited directory with the installed Pulsara launcher:

```bash
pulsara skills validate <skill-directory>
```

This checks files only; it does not install, enable or adopt them.
Report the real result. If `pulsara` is not on `PATH`, report standalone validation
as unavailable; do not use a repository `.venv`, `uv run`, or a private validator.
For an already authorized installation, use the running Host's
`INSTALL_LOOSE_SKILL`, which performs native validation; a separate preliminary
CLI validation is unnecessary. Follow `pulsara-skill-installer` for that flow;
do not describe authoring or a passed validation as installation.
