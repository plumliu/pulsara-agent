# Pulsara 目录契约：Codex-current-compatible Layout

本文定义 Pulsara 的用户级、项目级目录语义。这里的 “Codex-compatible” 指的是对齐 Codex 当前公开文档和本地实现共同形成的行为，而不是只采用文档里的理想子集。

## 1. 结论

Pulsara 第一轮 local skill discovery 读取四类 root：

```text
<workspace>/.pulsara/skills/<skill-name>/SKILL.md
<workspace>/.agents/skills/<skill-name>/SKILL.md
${PULSARA_HOME}/skills/<skill-name>/SKILL.md   # 默认 ~/.pulsara/skills
~/.agents/skills/<skill-name>/SKILL.md
```

语义边界：

```text
.pulsara = Pulsara product/config-layer 目录；也承载 Pulsara 自己安装或维护的 skills
.agents  = 跨 agent 共享 assets；主要承载可被多个 agent 消费的 skills
```

这修正了上一版文档的错误：`~/.codex/skills` 在 Codex 当前实现里确实仍被读取，远程 skill download 也写入 `$CODEX_HOME/skills/<skill-id>`。因此 Pulsara 不能把对应的 `~/.pulsara/skills` / `${PULSARA_HOME}/skills` 写成禁用路径。

## 2. Codex 对照

Codex 的配置层读取：

- `${CODEX_HOME}/config.toml`
- `${CODEX_HOME}/<profile>.config.toml`
- `<workspace>/.codex/config.toml`

Codex 的 skill roots 同时包含两类：

- 共享 roots：`<workspace>/.agents/skills`、父目录到 repo root 的 `.agents/skills`、`~/.agents/skills`。
- product/config-layer roots：project config folder 旁边的 `skills/`、user config folder 的 `${CODEX_HOME}/skills`，以及特殊的 `${CODEX_HOME}/skills/.system` system root。

Pulsara 对应为：

- `<workspace>/.agents/skills`
- `~/.agents/skills`
- `<workspace>/.pulsara/skills`
- `${PULSARA_HOME}/skills`，默认 `~/.pulsara/skills`

第一轮不实现 Codex 的 parent traversal、admin roots、plugin roots、system root；它们需要单独的 source/trust/permission 设计。

## 3. `~/.pulsara/`

`~/.pulsara/` 是 Pulsara 的用户级 product home，对标 Codex 的 `~/.codex/`。

建议内容：

```text
~/.pulsara/
  config.toml
  <profile>.config.toml
  auth.json
  credentials.json
  sessions/
  archived_sessions/
  attachments/
  logs/
  sqlite/
  cache/
  tmp/
  shell_snapshots/
  terminal/
  process_manager/
  supervisors/
  plugins/
  mcp/
  rules/
  skills/
    <skill-name>/
      SKILL.md
      references/
      scripts/
      assets/
```

约束：

- `${PULSARA_HOME}/skills` 是 Pulsara 可读的用户级 product skill root；未设置 `PULSARA_HOME` 时默认为 `~/.pulsara/skills`。
- dot 子目录不作为普通用户 skill 扫描；例如 `~/.pulsara/skills/.system` 未来可作为 system root 单独注入，但不能被普通 child scan 捞出来。
- `~/.pulsara` 里的配置、凭据、日志、会话等 runtime/private 文件不因为 `skills/` 可读而变成模型可随意读取的资产。

## 4. `~/.agents/`

`~/.agents/` 是用户级跨 agent asset home。

第一轮正式承认：

```text
~/.agents/
  skills/
    <skill-name>/
      SKILL.md
      references/
      scripts/
      assets/
```

约束：

- `~/.agents/skills` 下的 skill 是用户共享资产，不是 Pulsara 状态。
- Pulsara 可以读取和使用，但不应把自己的 runtime 文件写进去。
- 若 installer 提供“跨 agent 共享”安装，目标应是 `~/.agents/skills`；若提供“只给 Pulsara”安装，目标应是 `${PULSARA_HOME}/skills`。

## 5. `<workspace>/.pulsara/`

`<workspace>/.pulsara/` 是 Pulsara 项目级 product config / runtime artifact 目录，对标 Codex 的 `<workspace>/.codex/`。

建议内容：

```text
<workspace>/.pulsara/
  config.toml
  settings.local.toml
  terminal-output/
  cache/
  host/
  supervisor/
  logs/
  skills/
    <skill-name>/
      SKILL.md
      references/
      scripts/
      assets/
```

约束：

- `<workspace>/.pulsara/skills` 是 Pulsara 可读的项目级 product skill root。
- 跨 agent 可共享、可提交给其他 agent 使用的项目 skill 优先放 `<workspace>/.agents/skills`。
- 该目录下的敏感文件需要更严格的 read/write gate；不能因为 workspace 可读就让模型随意读 `.pulsara` 内部状态。

## 6. `<workspace>/.agents/`

`<workspace>/.agents/` 是项目级跨 agent asset root。

第一轮正式承认：

```text
<workspace>/.agents/
  skills/
    <skill-name>/
      SKILL.md
      references/
      scripts/
      assets/
```

约束：

- 存放可以提交到 repo 的项目级 shared skills。
- 表达“这个 skill 属于这个 workspace / repo / module”，而不是通过 frontmatter 手写 scope hash。
- 不存放 Pulsara 私有 session、logs、terminal output、auth 或 runtime cache。

## 7. Precedence

第一轮建议 root precedence：

1. `<workspace>/.pulsara/skills`
2. `<workspace>/.agents/skills`
3. `${PULSARA_HOME}/skills`
4. `~/.agents/skills`

同名 skill 只保留第一份，后续同名项产生 duplicate diagnostic。dot 子目录跳过普通扫描。

prompt 中的 `location` 必须是非绝对 display path，例如：

```text
.pulsara/skills/foo/SKILL.md
.agents/skills/foo/SKILL.md
~/.pulsara/skills/foo/SKILL.md
~/.agents/skills/foo/SKILL.md
```

不要把 host absolute path 放进 prompt。

## 8. 暂不做

第一轮不要做：

- 不把 capability / skill 写入图数据库作为节点。
- 不自动读取 `~/.codex/skills`、`~/.claude/skills`、`~/.hermes/skills` 或 `~/.openclaw/skills`。
- 不把其他 agent 的 product home 当作 Pulsara 的隐式 skill source。
- 不做 parent traversal。
- 不做 `/etc/pulsara/skills` admin roots。
- 不做 plugin / MCP-provided roots。
- 不把 `.agents` 变成 Pulsara 配置目录。

如果用户想迁移其他 agent 的 skills，应提供显式 migration / import 命令，而不是隐式扫别人的 home。
