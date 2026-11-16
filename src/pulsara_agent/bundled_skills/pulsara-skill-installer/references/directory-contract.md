# Loose Skill directories

Installation locations are:

| Scope | Destination |
| --- | --- |
| USER | `<effective Pulsara home>/skills/<name>/SKILL.md` |
| WORKSPACE | `<GUI project>/.pulsara/skills/<name>/SKILL.md` |

`PULSARA_HOME` selects the user home; its default is `~/.pulsara`.
The source directory can have any name. The installed directory uses the valid
`name` declared in `SKILL.md`.

## Source contents

`SKILL.md` starts with YAML frontmatter containing a name and description.
Names use lowercase letters, digits and single hyphens, up to 64 characters.
Optional portable fields are `license`, `compatibility` and string-to-string
`metadata`. The document is validated and normalized during import.

Ordinary supporting files and directories are copied, including `scripts/`,
`references/` and `assets/`. Loose directory import accepts ordinary files and
directories; a linked source tree can instead be prepared as an ordinary source
copy. `__pycache__` and `.DS_Store` are omitted. Installation leaves the source
in place and creates the destination only when it is absent.

## Installed copies

Use the returned `target.skill_path` for reading and editing. Resources are beside
that `SKILL.md`. Loose copies can be edited in place; later reads observe the
changes without reinstalling.

Pulsara also discovers project and user `.agents/skills` directories. Their Skills
can be read, edited and enabled or disabled. The removal action covers only
Pulsara home `skills` and project `.pulsara/skills`.

Loose copies take precedence over same-name Plugin and bundled Skills. Query the
particular copy's status when selection matters. Bundled defaults can be customized
by installing a same-name loose copy. Plugin Skill changes go through Plugin
source editing and replacement.

The capability page supports directory import and management. For explicitly
requested standalone administration, inspect `pulsara skills --help`. Changes made
outside the running app can be refreshed from the capability page or with
`reload_capabilities`.
