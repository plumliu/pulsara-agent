---
name: pulsara-skill-creator
description: Create, review or edit Agent Skills, including customizing an installed loose Skill or turning a repeated workflow into reusable instructions.
---

# Pulsara Skill Creator

## Choose the directory

Use the Skill's current SKILL Target; `skill_path` identifies its `SKILL.md`.
ROOT can find installed copies with `query_capabilities({kind:"SKILL"})` or
inspect one with `kind` and its Target. Scripts, references and assets are
relative to the directory containing the document.

Edit a loose Skill in place with file tools. Body and resource edits take effect
on subsequent reads without reinstalling or routine reload. Read the edited
`SKILL.md` with `read_file` to use its new instructions. Keep its name and location
when the task is a content edit.
For a new Skill, prepare a local source directory and follow
`pulsara-skill-installer` when installation is requested.

Plugin Skills are updated by editing the Plugin source and replacing its package;
follow `pulsara-plugin-installer`. To customize a bundled Skill, create and install
a loose copy with the same name. The loose copy takes precedence over the bundled
definition.

## Write the instructions

Use a directory matching the Skill name and start `SKILL.md` with:

```yaml
---
name: example-skill
description: Explain what the skill does and when to use it.
---
```

Names use lowercase letters, digits and single hyphens, up to 64 characters.
The description should identify the task and its trigger clearly. Optional
portable fields are `license`, `compatibility` and string-to-string `metadata`.

Keep the main workflow in `SKILL.md`. Put longer guidance in linked `references/`,
reusable helpers in `scripts/`, and templates or other resources in `assets/`.
Write concrete steps, useful defaults and examples. Include only the details
needed for the model to complete the task; link specialized cases where they
become relevant. Preserve useful existing instructions and resources when editing.

A Skill provides instructions and resources. Use a Plugin when the distribution
also needs MCP connections or Hooks.

## Validate and install

The installed launcher can validate a source directory:

```bash
pulsara skills validate <skill-directory>
```

For a requested installation, `INSTALL_LOOSE_SKILL` already validates the source;
a separate validation call is optional. If the launcher is unavailable, continue
with the file review and use the management tool for installation. Report authoring,
validation and installation as their actual separate outcomes.
