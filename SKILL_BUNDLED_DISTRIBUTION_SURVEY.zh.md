# Skill Bundled Distribution Survey

本文调研 Pulsara 下一步若要把“已验证可用的 SKILL 基建”提升为产品能力，应当如何分发和加载官方 / 内置 skills。范围聚焦三个本地代码库中已经存在并被生产路径消费的实现：

- `/Users/plumliu/Desktop/python_workspace/codex`
- `/Users/plumliu/Desktop/python_workspace/openclaw`
- `/Users/plumliu/Desktop/python_workspace/hermes-agent`

目标不是再做一轮抽象 capability survey，而是回答一个更窄、更承重的问题：

> “系统自带 skill 应该放在哪里、如何进入运行时、如何与用户安装 skill 共存、以及 Pulsara 这一轮最适合学谁？”

## 1. 结论先行

结论分三层：

1. **Codex** 采用的是“编译嵌入 + 运行时物化到 `${CODEX_HOME}/skills/.system` + 单独 `System` scope root”的方案。它最干净，但机制最重。
2. **Hermes** 采用的是“repo 自带 bundled skills，安装/更新时同步到 `~/.hermes/skills/`，再用 manifest / provenance / opt-out 机制管理”的方案。它没有单独 `.system` root，但产品可达性最直接。
3. **OpenClaw** 有 bundled / managed / workspace / plugin / marketplace 多来源技能系统，但它的重心是“多来源 runtime loading + marketplace / plugin / workspace”而不是一个简单的“系统 skill 缓存层”。

对 Pulsara 当前阶段的建议非常明确：

- **现在先做 Hermes-like，而不是 Codex-like。**
- 也就是：先把少量官方 skills 作为 package / repo 中的 bundled skills，同步到 `${PULSARA_HOME}/skills/`，再靠 manifest 和 provenance 区分“bundled 与 user skill”。
- **暂不引入 `${PULSARA_HOME}/skills/.system`**。那一层不是错误，但它会把复杂度前移到分发机制，而不是先完成产品能力。

一句话概括：

> Pulsara 现在需要的是“让官方 skill 真正可用”，不是“先把内置 skill 的分层做得最漂亮”。

## 2. Pulsara 当前基线

已核实的本仓库状态：

- Pulsara 已经支持 local skill discovery roots：
  - `<workspace>/.pulsara/skills`
  - `<workspace>/.agents/skills`
  - `${PULSARA_HOME}/skills`
  - `~/.agents/skills`
- 相关实现位于：
  - [local_skills.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/local_skills.py)
  - [resolver.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/resolver.py)
  - [render.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/render.py)
- 当前 **没有 bundled/system skill source**：
  - 没有 `${PULSARA_HOME}/skills/.system` 这样的特殊 root
  - 没有 package 内嵌技能物化逻辑
  - 没有 bundled skill manifest / provenance / opt-out / reset

因此这次调研的决策前提是：

- 不是“替换现有 roots”
- 而是“在现有 root 之上，为官方 skills 增加一个产品级分发和维护路径”

## 3. Codex：编译嵌入 + `.system`

### 3.1 源码中的内置来源

Codex 的内置 skills 直接来自源码树中的资源目录：

- [codex-rs/skills/src/assets/samples](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/skills/src/assets/samples)

这里有 `imagegen`、`openai-docs`、`plugin-creator`、`skill-creator`、`skill-installer` 等真实系统 skills。

`codex-rs/skills/src/lib.rs` 使用 `include_dir!` 将该目录编译进程序：

- [lib.rs:10](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/skills/src/lib.rs:10)

### 3.2 运行时落盘位置

Codex 启动时会把 embedded system skills 物化到：

```text
${CODEX_HOME}/skills/.system/
```

默认就是：

```text
~/.codex/skills/.system/
```

关键实现：

- [system_cache_root_dir](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/skills/src/lib.rs:17)
- [install_system_skills](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/skills/src/lib.rs:24)

它不是每次无脑重写，而是有 marker + fingerprint：

- `.codex-system-skills.marker`
- embedded dir 内容 hash 一致时跳过重复写入

### 3.3 运行时如何加载

Codex 的 `SkillsService` 创建时会安装 bundled system skills：

- [service.rs:77-99](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core-skills/src/service.rs:77)

loader 再把 `${CODEX_HOME}/skills/.system` 当作一个特殊 `System` scope root 插入 roots：

- [loader.rs:287-315](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core-skills/src/loader.rs:287)

root 合并顺序和 scope 排序是分开的，但都明确保留了 `System` 这个语义层：

- [root_loader.rs:93-104](/Users/plumliu/Desktop/python_workspace/codex/codex-rs/core-skills/src/root_loader.rs:93)

### 3.4 优点

- 系统技能与用户技能语义分层清楚。
- 可以非常明确地关闭 bundled skills，而不影响 user roots。
- 系统内置资源不依赖外部安装脚本是否成功复制。
- 同一个 `skills/` 父目录下虽然物理混放 user root 和 `.system`，但逻辑 scope 是分开的。

### 3.5 成本

- 需要 package embed / resource fingerprint / install-on-startup 机制。
- 需要单独 system scope root 逻辑。
- 需要 `.system` lifecycle：安装、禁用、清理、升级。
- 对 Pulsara 当前阶段来说，这些复杂度主要落在“分发层”而不是“skill 产品能力本身”。

### 3.6 结论

Codex 的方案是成熟且优雅的，但 **不是最小实现**。  
它适合“已经有稳定 product home、scope 分层、bundled lifecycle、packaged assets 管理”的系统。

## 4. Hermes：同步到活跃 skills 树

### 4.1 源码中的 bundled skills

Hermes 也在 repo 中维护 bundled skills：

- `skills/` 目录

文档写得很直接：

- [CONTRIBUTING.md:194](/Users/plumliu/Desktop/python_workspace/hermes-agent/CONTRIBUTING.md:194)
  - `skills/                   # Bundled skills (copied to ~/.hermes/skills/ on install)`

### 4.2 安装/更新时的同步目标

Hermes 不引入单独 `.system` root，而是把 bundled skills 同步到：

```text
~/.hermes/skills/
```

安装脚本中的说明非常明确：

- [install.sh:1698-1717](/Users/plumliu/Desktop/python_workspace/hermes-agent/scripts/install.sh:1698)

它会：

1. 创建 `~/.hermes/skills`
2. 调用 `tools/skills_sync.py`
3. 如果 Python sync 失败，退回到简单 copy fallback

### 4.3 Manifest 和更新语义

Hermes 的核心不是“系统 root”，而是“manifest-based sync”：

- [skills_sync.py](/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/skills_sync.py)

它维护：

```text
~/.hermes/skills/.bundled_manifest
```

格式是：

```text
skill_name:origin_hash
```

语义：

- 新 bundled skill：复制并记 manifest
- 已同步 skill：
  - 若用户副本仍等于 manifest hash，说明用户没改，可以安全更新
  - 若用户副本不等于 manifest hash，说明用户改过，跳过
- 用户删掉的 bundled skill：尊重删除，不自动恢复
- repo 中已移除的 bundled skill：从 manifest 清理

这其实是 Hermes-like 方案最承重的点：

> 内置 skill 和用户 skill 可以共处于同一个活跃树，但是否“还算 bundled / 是否该更新 / 是否该恢复”，由 manifest 和 provenance 决定，而不是由目录层级决定。

### 4.4 Opt-out

Hermes 支持 profile / install 级别的 bundled skills opt-out：

- marker 文件：`.no-bundled-skills`
- 安装时 `--no-skills`
- 后续 update / sync 都尊重该 marker

关键实现：

- [install.sh:1700-1708](/Users/plumliu/Desktop/python_workspace/hermes-agent/scripts/install.sh:1700)
- [profiles.py:883](/Users/plumliu/Desktop/python_workspace/hermes-agent/hermes_cli/profiles.py:883)
- [skills_sync.py](/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/skills_sync.py)

### 4.5 Restore / reset / provenance

Hermes 还把“bundled / optional / hub-installed / agent-created”混住之后的治理问题补上了：

- `.bundled_manifest`
- `.hub/lock.json`
- `.archive/`
- restore-backups
- `skills reset`
- `restore_official_optional_skill`

参考：

- [skills_hub.py:1080+](/Users/plumliu/Desktop/python_workspace/hermes-agent/hermes_cli/skills_hub.py:1080)
- [skill_usage.py](/Users/plumliu/Desktop/python_workspace/hermes-agent/tools/skill_usage.py:159)

这说明 Hermes 并不是“简单复制一下就完事”。  
它的简化点在于 **不额外发明 `.system` root**，而不是不做治理。

### 4.6 优点

- 路径直观：所有 active skills 都在 `~/.hermes/skills/`。
- 产品可达性强：安装后立刻进入真实运行路径。
- 实现顺序自然：先有 repo 自带 skills，再有 sync，再有 manifest/provenance，再加 restore/governance。
- 不需要先解决 system/user scope root 分层问题。

### 4.7 成本

- bundled 与 user skills 混住，语义要靠 sidecar metadata 管。
- reset / restore / update 逻辑比 Codex 的“纯 `.system` cache”更重要。
- 如果 manifest/provenance 做不好，很容易把用户改过的 skill 覆盖掉。

### 4.8 结论

Hermes 的方案是 **产品优先、机制后补齐**。  
它很适合 Pulsara 当前阶段，因为 Pulsara 现在最缺的不是漂亮的 scope hierarchy，而是：

- 官方 skill 能被发给用户
- 发出去之后能自动发现
- 更新时不踩用户改动
- 用户能选择不要 bundled skills

## 5. OpenClaw：多来源技能系统，不是简单 `.system`

### 5.1 Bundled skills 的存在

OpenClaw 也有 repo 内 skills：

- [openclaw/skills](/Users/plumliu/Desktop/python_workspace/openclaw/skills)

README / VISION 也明确说：

- [README.md:169](/Users/plumliu/Desktop/python_workspace/openclaw/README.md:169)
- [VISION.md:89](/Users/plumliu/Desktop/python_workspace/openclaw/VISION.md:89)

它确实有 bundled skills 用于 baseline UX。

### 5.2 运行时定位 bundled root

OpenClaw 没有 Codex 式 `.system` cache，而是运行时解析 bundled skills dir：

- [bundled-dir.ts](/Users/plumliu/Desktop/python_workspace/openclaw/src/skills/loading/bundled-dir.ts)

它会：

- 先看 `OPENCLAW_BUNDLED_SKILLS_DIR`
- 再看打包产物旁边的 `skills/`
- 再从 package root 反推 `<packageRoot>/skills`

### 5.3 更大的技能来源矩阵

OpenClaw 的重点不是“系统 skill 单独落盘”，而是多来源合并：

- bundled
- workspace
- managed
- personal roots
- plugin skills
- marketplace / ClawHub

相关实现集中在：

- [workspace.ts](/Users/plumliu/Desktop/python_workspace/openclaw/src/skills/loading/workspace.ts)

它还引入了：

- bundled allowlist
- prompt visibility 过滤
- skillFilter
- session snapshot
- plugin skill dirs
- remote eligibility

### 5.4 结论

OpenClaw 给 Pulsara 的启发主要是：

- skills 终局会是一个多来源 runtime system
- bundled 只是其中一个来源

但 **它不提供一个比 Hermes 更简单的“内置 skill 起步方案”**。  
对于 Pulsara 这一轮，“照 OpenClaw 做”会把我们直接带到一个比现在需求更大的分发体系里。

## 6. 三种模型的结构对比

| 系统 | 官方 skill 来源 | 运行时落盘位置 | 是否单独 system root | bundled 与 user 是否混住 | 更新/覆盖依据 |
| --- | --- | --- | --- | --- | --- |
| Codex | 编译嵌入 `src/assets/samples` | `${CODEX_HOME}/skills/.system` | 是 | 否，逻辑上分层 | embedded fingerprint + marker |
| Hermes | repo `skills/` | `~/.hermes/skills/` | 否 | 是 | `.bundled_manifest` + hash + provenance |
| OpenClaw | repo `skills/` / packaged skills | 运行时解析 `skills/` roots | 不强调 `.system` | 多来源合并 | source-specific runtime logic |

## 7. Pulsara 应该学谁

### 7.1 为什么不是 Codex-like

Codex-like 不是错，而是 **时机还没到**。

如果 Pulsara 现在立刻引入：

- `${PULSARA_HOME}/skills/.system`
- package embedded resource
- startup materialization
- `System` scope root
- bundled enable/disable lifecycle

那我们会先花主要精力在：

- package data 管理
- fingerprint/marker
- `.system` 物化和清理
- scope/root 设计

而不是先把真正想发给用户的官方 skill 产品化。

这和我们之前反复踩过的模式很像：

> 先做结构最漂亮的抽象，再发现真正的 runtime / product 消费点还没站稳。

### 7.2 为什么是 Hermes-like

Hermes-like 刚好满足 Pulsara 当前最真实的需求：

1. **可达性优先**
   - 官方 skill 一旦同步到 `${PULSARA_HOME}/skills`，它立刻通过现有 runtime roots 被发现。

2. **与当前目录契约一致**
   - Pulsara 已经正式承认 `${PULSARA_HOME}/skills` / `~/.pulsara/skills` 是 product skill root。
   - 这正好是最自然的 bundled sync 目标。

3. **不需要改现有 skill discovery 语义**
   - 不必为了 system root 先改 `SkillSource`、scope、catalog、display path。

4. **避免提前规划 `.system`**
   - 本轮不把 Codex-like `.system` 当成后续路线承诺。
   - 若未来真的需要更硬的官方 / 用户隔离，应重新做独立设计，而不是让本轮 Hermes-like sync 预埋一半 `.system` 语义。

## 8. 对 Pulsara 的直接启发

Pulsara 这轮应当承认两个事实：

1. **系统级 skill 是产品资产，不只是测试样例。**
   - 现在已经通过 `pulsara-skill-installer` 和 `hello-pulsara-skill` 证明了 runtime 能工作。
   - 下一步要解决的是“如何把 installer 变成每个用户天然拥有的官方 skill”。

2. **这不是图数据库问题，也不是 capability ontology 问题。**
   - bundled skill 分发首先是 package / home dir / manifest / sync 问题。
   - 不需要先把 capability 写进图或拉进 ontology 才能落地。

## 9. 本轮选择

基于本地实现的真实对比，本轮建议固定为：

- **Survey 结论：Pulsara 采用 Hermes-like bundled skill distribution。**
- 官方 bundled skills 同步到 `${PULSARA_HOME}/skills`。
- 使用 manifest / provenance / opt-out 管理 bundled 生命周期。
- 继续保留现有四个 skill roots。
- 暂不增加 `${PULSARA_HOME}/skills/.system`。

下一篇实施文档将据此给出 Pulsara 的 Hermes-like 方案。
