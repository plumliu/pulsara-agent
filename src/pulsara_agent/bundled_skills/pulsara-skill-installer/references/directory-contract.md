# Pulsara Loose Local Skill Directory Contract

Official destinations are:

```text
<workspace>/.pulsara/skills/<skill-name>/SKILL.md
${PULSARA_HOME}/skills/<skill-name>/SKILL.md
```

The production Runtime discovers these roots together with workspace and user `.agents/skills` roots. Direct user copy, edit, rename, and deletion remain valid filesystem paths; no receipt or marker makes a Skill authoritative.

## Skill Folder Rules

- The folder name must match the `name` in `SKILL.md`.
- `SKILL.md` must begin with YAML frontmatter.
- The frontmatter must include string fields named `name` and `description`.
- Skill names use lowercase letters, digits, and hyphens only.
- Optional `license`, `compatibility`, and bounded string-to-string `metadata` remain portable data.
- Ordinary files and directories are copied, including useful `scripts/`, `references/`, and `assets/` resources.
- Symlinks, sockets, devices, FIFOs, bundled provenance, `__pycache__`, and `.DS_Store` are outside the official loose-copy domain.

The official installer validates and copies one frozen source observation into a hidden sibling stage, verifies exact contents and portable modes, and publishes with the platform's exclusive no-replace directory rename. Existing destinations are never overwritten. The exclusive primitive guarantees final-name no-replace within the held root; it is not a sandbox or a power-loss durability promise, and same-UID replacement of the private stage or target-root namespace after the final binding cut is outside the product concurrency contract.

After installation, use `pulsara skills list` for effective winners and `pulsara skills doctor` for invalid, shadowed, or unavailable candidates. Filesystem changes become model-visible only at the next legal provider safe point.
