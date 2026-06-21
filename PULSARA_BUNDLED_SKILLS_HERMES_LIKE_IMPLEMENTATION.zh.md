# Pulsara Bundled Skills Hermes-like 实施文档

本文基于 [SKILL_BUNDLED_DISTRIBUTION_SURVEY.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/SKILL_BUNDLED_DISTRIBUTION_SURVEY.zh.md) 的结论，定义 Pulsara 第一轮官方 / 内置 skill 分发方案。

核心决策只有一句话：

> Pulsara 这一轮不做 Codex-style `${PULSARA_HOME}/skills/.system`，而是做 Hermes-like：把 repo/package 中的 bundled skills 同步到 `${PULSARA_HOME}/skills`，并用 manifest / provenance / opt-out 管理它们。

本文不规划 `.system` 分层；如果未来确实需要更细的官方 / 用户隔离，应另开设计，而不是把它混进本轮 Hermes-like sync。

## 0. 总目标

V1 要做到：

1. Pulsara 自带一小组官方 skills。
2. `host run` / `host repl`、显式 sync、或未来 update 流程中，官方 skills 能同步到 `${PULSARA_HOME}/skills`。
3. 同步不会覆盖用户已经修改过的 skill。
4. 用户可以 opt out，不要 bundled skills。
5. 用户可以查看哪些 skills 是 bundled。
6. 用户可以恢复某个 pristine bundled skill。
7. 所有这些机制都建立在 **现有四个 local roots** 之上，不额外发明新的 runtime root 层次。

V1 不做：

- `${PULSARA_HOME}/skills/.system`
- `System` scope root
- plugin/MCP-provided bundled skill roots
- automatic marketplace / remote skill install
- capability 入图
- “从流程中长出 skill” 的 capability-as-output
- bundled optional skills catalog

## 1. 路径与来源契约

### 1.1 现有 runtime roots 保持不变

Pulsara 当前 skill roots 保持：

```text
<workspace>/.pulsara/skills
<workspace>/.agents/skills
${PULSARA_HOME}/skills
~/.agents/skills
```

其中 bundled skills 的默认同步目标是：

```text
${PULSARA_HOME}/skills
```

若 `PULSARA_HOME` 未设置，则等价于：

```text
~/.pulsara/skills
```

### 1.2 官方 bundled skills 的源码来源

第一轮建议在 package 内放一个明确的 bundled skills 源目录，例如：

```text
src/pulsara_agent/bundled_skills/
  pulsara-skill-installer/
    SKILL.md
    references/
    scripts/
  pulsara-skill-creator/
    SKILL.md
    references/
    scripts/
```

选择理由：

- 它表达的是“随 Pulsara 发布的官方 product assets”
- 不和 `src/pulsara_agent/capability/`、`runtime/`、`entities/` 混在一起
- 比临时放在仓库根目录更容易纳入 wheel/package data

**不要**把测试项目里的 `pulsara_test/.pulsara/skills` 直接当成正式分发路径。那只是 dogfood fixture。

### 1.3 包装约束

当前 `pyproject.toml` 声明了 `packages = ["src/pulsara_agent"]`。正式实现时必须实际 build wheel 并验证 `bundled_skills/` 进入 wheel；如果未来构建配置收窄到只包含 Python 文件，再补 package data / force-include。

实现方式取决于 Hatch 当前打包行为，但原则必须固定：

- bundled skills 必须能从安装后的 Pulsara 包中读取
- 不能只依赖源码 checkout 场景

也就是说，未来安装自 pip/wheel 的 Pulsara 也必须能 sync 官方 skills。

## 2. 运行时模型

### 2.1 为什么选 sync，而不是 runtime 直接读 package 目录

Pulsara 第一轮应当像 Hermes 一样，把 bundled skill 先同步到活跃 skills tree，再让现有 runtime 去读。

不要让 runtime special-case：

- “如果用户 root 里没有，就去 package 里读 embedded/bundled root”

原因：

1. 会产生两套 source of truth：
   - 一套是 package 内的只读 bundled 目录
   - 一套是 `${PULSARA_HOME}/skills` 下的 live tree
2. 用户通过 installer / skill authoring / 手动 patch 修改 live skill 时，会和 package side 形成二义性
3. 现有 runtime 已经只认 local filesystem skill roots，没必要现在再引入 package-side bypass

Hermes 的经验在这里很直接：

> 先把 bundled skill 变成 live skills tree 的一部分，再用 manifest 管它是不是官方来源。

### 2.2 第一轮同步入口

建议至少有三个入口：

1. **显式命令**
   - 未来可加 `pulsara skills sync-bundled`

2. **真正运行入口的 best-effort sync**
   - 在 `host run` / `host repl` 这类会启动长会话或执行模型调用的入口前执行
   - sync 失败只产生 warning/diagnostic，不阻塞正常运行

3. **未来 update 流程**
   - Pulsara 以后若有 `pulsara update` / 安装器，那里也应触发 bundled sync

当前阶段如果还没有完整 installer/update 体系，优先做：

- 一个纯 Python 的 `sync_bundled_skills(...)`
- 以及 `host repl` / `host run` 之前的 best-effort 调用

`host inspect` 必须保持只读。它可以报告当前 `${PULSARA_HOME}/skills` 中已存在的 bundled skills、manifest 状态、以及“官方 bundled skills 尚未同步”的 diagnostic，但不能为了补齐 catalog 而写入 `${PULSARA_HOME}`。这样 `host inspect` 仍然能在无 API key、无网络、无副作用的环境下作为纯检视命令使用。

## 3. Manifest 契约

### 3.1 Manifest 文件

第一轮建议仿照 Hermes，在 `${PULSARA_HOME}/skills` 下维护一个 sidecar manifest：

```text
${PULSARA_HOME}/skills/.bundled_manifest
```

格式建议为：

```text
skill_name:origin_hash
```

其中 `origin_hash` 是 bundled source skill directory 的内容 hash。

### 3.2 Manifest 语义

sync 逻辑必须满足：

1. **新 bundled skill**
   - manifest 中没有
   - 目标目录不存在
   - 复制到 `${PULSARA_HOME}/skills/...`
   - 记录 `skill_name:origin_hash`

2. **已同步且用户未修改**
   - manifest 中有 `origin_hash`
   - 目标目录当前 hash 仍等于 `origin_hash`
   - 如果 package 中的 bundled source 变了，可以安全更新

3. **已同步但用户已修改**
   - manifest 中有 `origin_hash`
   - 目标目录当前 hash 不等于 `origin_hash`
   - 跳过，不覆盖用户改动

4. **用户主动删除**
   - manifest 中有
   - 目标目录不存在
   - 不自动恢复

5. **repo / package 中已移除该 bundled skill**
   - manifest 中有，但 source 不再存在
   - 从 manifest 清理

这五条是 Hermes-like 方案最核心的行为约束。

### 3.2.1 已知限制：existing unmanaged collision 没有自动逃生口

第一轮还有一个明确的安全优先限制：

- 如果 `${PULSARA_HOME}/skills/<name>/` 下已经存在一个**非 bundled 管理**的同名目录
- 而 package source 中也恰好有同名官方 bundled skill

那么 sync 应返回：

```text
skipped_existing_unmanaged
```

并且不会覆盖该目录。

这意味着：如果用户手动放了一个与官方 skill 同名的目录，例如手动创建了 `${PULSARA_HOME}/skills/pulsara-skill-installer/`，那么官方同名 skill 会被永久挡住；而 `reset <name>` 也应返回 “not_bundled”，不会接管该目录。

这是 V1 的有意选择：

- **优先不 clobber 用户内容**
- 即便代价是“官方 skill 暂时无法落地”

这个行为必须在文档中视为已知限制，而不是隐式陷阱。

未来如果要给用户一个显式逃生口，应单独设计，例如：

- `pulsara skills reset <name> --adopt`
- `pulsara skills sync-bundled --force <name>`

但这些都不属于本轮。

### 3.3 写入方式

manifest 必须原子写入：

- temp file
- fsync
- rename / replace

不要直接覆盖写。  
原因和 memory ledger、timeline sidecar 一样：崩溃中断会留下半写文件。

## 4. Provenance 契约

单有 manifest 还不够；manifest 只能回答“这个目录原本是不是 bundled sync 过”，不能表达更丰富的来源语义。

第一轮建议为每个已同步的 bundled skill 增加一个轻量 provenance sidecar，例如：

```text
${PULSARA_HOME}/skills/<skill-name>/.pulsara-skill-source.json
```

内容可以很小：

```json
{
  "source": "bundled",
  "bundled_from": "pulsara-agent",
  "bundled_version": "0.1.0",
  "origin_hash": "..."
}
```

V1 中这个 sidecar 的职责只有两个：

1. 给 inspect / CLI 解释“这是官方 bundled skill”
2. 为后续 reset/restore 提供更直观的判断依据

**不要**把它做成 capability graph entry，也不要把它纳入 ontology 映射。  
这里就是一个分发 sidecar。

## 5. Opt-out 契约

### 5.1 Marker 文件

仿照 Hermes，第一轮使用一个 profile/home 级 marker：

```text
${PULSARA_HOME}/.no-bundled-skills
```

存在时：

- `host run` / `host repl` 启动前的 bundled sync 不执行
- 显式 sync 命令除非带 override，否则也不执行

### 5.2 V1 语义

V1 的 opt-out 只表达：

> “不要再向 `${PULSARA_HOME}/skills` 注入官方 bundled skills”

它**不自动删除已经存在的 skills**，除非用户显式要求“移除 pristine bundled skills”。

这是 Hermes 的好经验：  
opt-out 和 destructive removal 是两步，不要混在一次操作里。

## 6. Reset / Restore 契约

Hermes-like 方案如果没有 restore，长期维护会很痛苦。  
但 Pulsara 第一轮不需要把 restore 做成大而全的 curator 子系统，只需要做最小恢复面。

### 6.1 `reset bundled skill`

建议第一轮保留一个最小动作：

```text
pulsara skills reset <name>
```

语义：

- 若该 skill 是 bundled skill
- 且目标目录存在、但内容已偏离当前 bundled source
- 则先备份当前目录，再用 bundled source 覆盖重建

### 6.2 备份位置

建议：

```text
${PULSARA_HOME}/skills/.restore-backups/<timestamp>/<skill-name>/
```

原因：

- 不污染正常 skill roots
- 不引入复杂 archive/curator 语义
- 能满足“覆盖前先保留用户版本”

### 6.3 V1 不做的恢复行为

V1 不做：

- `.archive/` 生命周期
- curator
- 自动 prune bundled skills
- cross-skill consolidation

这些都是 Hermes 后期治理层，不是 Pulsara bundling 第一轮的必要条件。

## 7. 与现有 skill roots 的关系

### 7.1 为什么 bundled 目标是 `${PULSARA_HOME}/skills`

不是 `~/.agents/skills`，因为：

- bundled skill 是 **Pulsara 产品资产**
- 不是天然的“跨 agent 共享资产”

`.agents/skills` 更适合：

- 用户显式想跨多个 agent 共享的 skills
- 第三方 skill / imported skill / community skill

`.pulsara/skills` 更适合：

- Pulsara 官方提供并维护的 skills
- Pulsara 自己的安装器、creator、repo-review 等产品工作流

### 7.2 为什么不做 `<workspace>/.pulsara/skills` 的 bundled sync

因为 bundled skill 是用户级产品能力，而不是某个 workspace 的私有素材。

把官方 skill 注入 workspace root 会带来两个问题：

1. 每个项目都各自复制一份官方 skill，升级/维护更麻烦
2. 用户会误以为这些 skills 是项目资产，可被版本控制、提交、分叉

官方 bundled skills 更适合待在 `${PULSARA_HOME}/skills`。

## 8. 与当前 capability runtime 的关系

Hermes-like bundling 是一个**分发层补丁**，不应改变现在 capability runtime 的核心契约。

特别是：

- 仍然由 `LocalSkillProvider` 扫描 skill roots
- 仍然由 `LocalSkillResolver` resolve catalog / active injections
- 仍然由现有 render/activation 机制把 skills 暴露给模型

也就是说：

> bundled sync 的工作是“把官方 skill 复制到一个 runtime 已经会扫描的 root”，而不是“再发明一套官方 skill 的 runtime 旁路”。

这点非常重要。

## 9. 对 Codex-like `.system` 的明确延后

为了避免后续实现中反复摇摆，本文明确写死本轮不做：

### 9.1 不做 `${PULSARA_HOME}/skills/.system`

理由：

- 现阶段没有必要为少量官方 skills 引入新 root
- 会增加 runtime root/type/scope 复杂度
- 会把实现重心从“skill 产品化”挪到“分发分层”

### 9.2 不引入 `SkillSource = "system"`

第一轮可以把当前 `SkillSource = Literal["workspace", "user"]` 扩展为：

```python
SkillSource = Literal["workspace", "user", "bundled"]
```

但不要引入 `system`。

`bundled` 表达的是“该 skill 来自 Pulsara 官方 bundled sync，并落在 `${PULSARA_HOME}/skills` 这个 user product root 中”。它是来源/provenance 语义，不是新的 runtime root 层次。`system` 则会暗示 Codex-like `.system` root 和独立 scope，本轮明确不做。

### 9.3 不让 graph / ontology 参与分发判断

bundled skill 是否存在、是否更新、是否恢复，只取决于：

- package bundled source
- `${PULSARA_HOME}/skills`
- `.bundled_manifest`
- provenance sidecar
- opt-out marker

不要引入图数据库或 capability JSON-LD 作为 source of truth。

## 10. 建议的实现拆分

### PR1：bundled skill source + sync library

目标：

- 增加 `src/pulsara_agent/bundled_skills/`
- 增加纯函数库，例如 `bundled_skills.py`
- 实现：
  - resolve bundled source dir
  - compute skill dir hash
  - read/write `.bundled_manifest`
  - sync bundled skills into `${PULSARA_HOME}/skills`
  - opt-out marker check

不改 CLI，不改 host wiring。

### PR2：run/repl best-effort sync，inspect 保持只读

目标：

- 在 `host repl`、`host run` 之前调用 best-effort sync
- sync 失败只给 warning/diagnostic，不阻塞整个 host
- `host inspect` 不执行 sync，只读取当前状态并可报告“bundled skills 未同步”的 diagnostic

原因：

- 先保证 bundled skill 能在真实用户路径中出现
- 不必先等待未来完整 installer/update 体系
- 保留 `host inspect` 的纯只读契约：无 API key、无网络、无写入副作用时也能工作

### PR3：status / reset CLI

目标：

- 增加最小 CLI：
  - `pulsara skills sync-bundled`
  - `pulsara skills status`
  - `pulsara skills reset <name>`
  - 可能再加 `pulsara skills opt-out` / `opt-in`

V1 不需要做很大的 interactive TUI。

### PR4：官方 bundled skills 首批落地

建议第一批只放少量高价值系统 skills：

- `pulsara-skill-installer`
- `pulsara-skill-creator`
- 也许再加一个 `pulsara-repo-review`

不要一上来塞很多。  
先用真正高频、会自举 skill 生态的几个官方 skills 证明机制。

## 11. 测试策略

### 11.1 必须补的非 LLM 回归测试

这一层非常适合写稳定回归测试，不依赖 real LLM：

1. source skill 同步到 `${PULSARA_HOME}/skills`
2. second sync 在 unchanged 情况下 no-op
3. user-modified skill 不被覆盖
4. user-deleted bundled skill 不被自动恢复
5. opt-out marker 存在时 sync no-op
6. `reset <name>` 会先备份再恢复
7. sync 后 skill 仍能被现有 `LocalSkillProvider` 发现

### 11.2 LLM 测试继续保留为 smoke

这次 dogfood 已经证明：

- installer skill 可被发现
- 根目录待安装 skill 可被安装
- 新 turn 中已安装 skill 可被激活、读 references、执行 scripts

未来可以把这类测试保留为少量 real-LLM smoke，而不要把 bundling correctness 建立在 LLM 上。

## 12. 风险与边界

### 12.1 最大风险：误覆盖用户技能

这是 Hermes-like 方案最大的真实风险。  
因此 manifest + hash 判断不是可选项，而是这个方案的核心安全边界。

### 12.2 第二风险：让 bundled sync 变成隐式写操作惊喜

如果 `host run` / `host repl` 一启动就默默往 `${PULSARA_HOME}/skills` 写东西，用户可能会惊讶。

因此建议：

- 第一次 sync 时写清楚 diagnostics / CLI 提示
- 文档明确说明 Pulsara 会向 `${PULSARA_HOME}/skills` seed 官方 skills
- 后续可加 opt-out

### 12.3 不要把它包装成“system scope”

如果我们现在一边做 Hermes-like，同步到 `${PULSARA_HOME}/skills`，一边在文档里把它说成“system skill scope”，实现和语义就会开始漂。

本轮必须保持诚实：

- 路径上它就是 user product root
- 官方来源靠 manifest/provenance 识别
- 不是独立 `.system` root

## 13. 最终决策

本轮 Pulsara bundled skill 分发明确采用：

- **Hermes-like active-tree sync**
- 目标根：`${PULSARA_HOME}/skills`
- sidecars：
  - `.bundled_manifest`
  - `.no-bundled-skills`
  - per-skill provenance file
- 最小治理：
  - sync
  - status
  - opt-out
  - reset/restore backup

明确不采用：

- Codex-like `${PULSARA_HOME}/skills/.system`
- runtime special-case package bundled root
- capability graph / ontology 驱动的分发逻辑

换句话说：

> Pulsara 先像 Hermes 一样，把官方 skills 送到用户真正会被 runtime 扫描的活跃树里；本轮不引入 Codex 那种更重的 `.system` 分层。
