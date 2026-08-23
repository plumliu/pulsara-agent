# Round 9.3：Agent Plugin Bundle、Hook Adapter 与 Subagent Preset 实施规格

> 状态：**DEFERRED — 暂不实施，NOT ACTIVATED**
>
> 本规格当前仅保留为后续讨论材料，不是可交给coding agent的实施authority。Plugin与Hook两项产品能力均已暂缓；Round 9.2重新闭合并明确恢复实施前，不得据本文编码Plugin store、Hook adapter、Subagent preset或任何过渡兼容结构。
>
> 修订日期：2026-08-22
>
> 编码前置：Round 9、Round 9.1、Round 5B、Round 10 均已 **ACTIVATED**，且[Round 9.2 independent Hook subsystem](ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md)必须先ACTIVATED。本文只能复用它们当前的 Skill 四根目录、MCP direct/meta、shared cold-epoch assembler、compaction、ROOT-orchestrated worker task graph与唯一`KernelHookDispatcher`；不得恢复旧 registry、第五个 Skill root、compaction-private capability planner、第二套 subagent runtime或Plugin-private Hook engine。
>
> Fingerprint 约束：[`PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md`](PULSARA_FINGERPRINT_SUBTRACTION_HARD_CUT_IMPLEMENTATION_SPEC.zh.md) 是本文的强制上位契约。完整 frozen value 已传到消费者时不得再添加 DTO fingerprint。本文复用Round 9.2既有Hook trust digest；本轮只新增Skill安装provenance content digest，以及opaque agent-preset ref/directory cursor MAC等真正跨边界的摘要。
>
> 上位契约：[Round 3 structured compiler](ROUND_3_STRUCTURED_MODEL_INPUT_COMPILER_IMPLEMENTATION_SPEC.zh.md)、[Round 3.1 prefix continuity](ROUND_3_1_PROVIDER_INPUT_PREFIX_CONTINUITY_IMPLEMENTATION_SPEC.zh.md)、[Round 5A execution envelope](ROUND_5_LONG_HORIZON_EXECUTION_ENVELOPE_IMPLEMENTATION_SPEC.zh.md)、[Round 5B compaction](ROUND_5B_LONG_HORIZON_CONTEXT_COMPACTION_IMPLEMENTATION_SPEC.zh.md)、[Round 6 MCP](ROUND_6_MCP_PRODUCTION_CAPABILITY_IMPLEMENTATION_SPEC.zh.md)、[Round 7 observation](ROUND_7_MODEL_VISIBLE_FAILURE_AND_TOOL_OBSERVATION_IMPLEMENTATION_SPEC.zh.md)、[Round 7.1 ToolResult projection](ROUND_7_1_PROVIDER_VISIBLE_TOOL_RESULT_PROJECTION_IMPLEMENTATION_SPEC.zh.md)、[Round 9 unified capability](ROUND_9_UNIFIED_CAPABILITY_SEMANTICS_IMPLEMENTATION_SPEC.zh.md)、[Round 9.1 Agent Skills](ROUND_9_1_AGENT_SKILLS_STANDARD_IMPLEMENTATION_SPEC.zh.md)、[Round 10 subagent graph](ROUND_10_HIERARCHICAL_SUBAGENT_ORCHESTRATION_IMPLEMENTATION_SPEC.zh.md)、[Gap Index](archived_docs/POST_HARD_CUT_PRODUCT_CAPABILITY_GAP_INDEX.zh.md)
>
> 公开兼容基线：[Agent Plugins Specification 1.0.0](https://agent-plugins.org/specification)（Published）、[Codex Plugin packaging](https://developers.openai.com/plugins/build/plugins)、[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、[Claude Code Plugins](https://code.claude.com/docs/en/plugins)、[Claude Code Subagents](https://code.claude.com/docs/en/sub-agents)

本文把 Plugin 定义为**可安装、可启用、可诊断的组件组合包**。Plugin 不是第四种 capability leaf，不拥有通用 `invoke()`，也不拥有 Skill、MCP、Tool、Subagent 或 canonical conversation 的第二套 authority。

本轮支持四类包内贡献：

1. Agent Skills；
2. MCP server definitions；
3. Codex-compatible lifecycle Hooks；
4. Claude-compatible、受限的 Subagent presets。

其中前两类进入现有 Round 9/9.1 owner；Hook只经adapter进入Round 9.2已经激活的process-local lifecycle engine；Subagent preset只为Round 10现有worker task提供可选、低authority启动说明，不改变其拓扑、权限、工具或durable task model。

---

## 0. 最终产品形状

### 0.1 Plugin 只负责组合

```text
InstalledPluginPackage
  ├─ Skill directories
  │    -> materialize into one existing Pulsara Skill root
  │    -> LocalSkillProvider performs ordinary complete scan
  ├─ MCP server configs
  │    -> merge into the one existing resolved MCP config inventory
  │    -> MCP supervisor performs ordinary reload / direct-meta routing
  ├─ Hook definitions
  │    -> Plugin Hook contribution adapter normalizes package config/provenance
  │    -> Round 9.2 generic trust decides runnable disposition
  │    -> one generic process-local Hook dispatcher and executor
  └─ Subagent presets
       -> list_agent_presets issues exact opaque refs
       -> Round 10 child cold seed consumes one optional preset body
```

Plugin 不回答：

- Tool 是否获得执行 permission；
- MCP slot 是否 READY；
- Skill 是否应该被模型采用；
- Hook 是否可以改写 canonical rows；
- worker 是否能递归创建 worker；
- Plugin 是否可以把任意 Python 模块加载进 Host 进程。

### 0.2 四类贡献的 authority

| 组件 | Provider 暴露 | 最终 authority | Plugin 可做什么 |
|---|---|---|---|
| Skill | 现有 `SKILL_CATALOG` + ordinary `read_file` | Round 9.1 LocalSkillProvider 与 filesystem | 原样物化目录并记录安装 provenance |
| MCP | existing DIRECT tool 或 new-MCP meta route | Plugin enable授权server spawn/connect；Round 6/9 supervisor、slot、tool permission/effect与attempt拥有后续调用 | 提供普通 `McpServerConfig` 输入 |
| Hook | 成功的 `additionalContext` 仅形成 append-only `UNTRUSTED_OBSERVATION` | lifecycle operation仍归User/Tool/Permission/Compaction/Subagent原owner；Round 9.2 generic trust/dispatcher拥有trust与process-local attempt | Plugin adapter贡献已归一化definitions/provenance/environment；不拥有trust或dispatcher |
| Subagent preset | ROOT 通过目录工具取得 opaque ref；child 启动时得到低 authority preset body | Round 10 task/coordinator/cold assembler | 提供 name、description 与正文；不拥有 executor |

### 0.3 Hook vocabulary完全继承Round 9.2

Round 9.3不声明新的Hook event。Plugin package中的Codex/Claude Hook names只经adapter映射到Round 9.2已ACTIVATED的11项`HookEventType`；unknown/unsupported event在最窄entry上typed skip。Plugin不能复用`CommittedEventType`/`LiveEventType`，也不能扩展generic control vocabulary。

### 0.4 Prefix 不变量

同一 exact ROOT/child scope、同一 continuity epoch：

```text
SYSTEM[n + 1]   == SYSTEM[n]
tools[n + 1]    == tools[n]
messages[n + 1] == messages[n] || append_only_suffix
```

因此：

- Plugin enable/disable 不热改 `BASE_SYSTEM` 或 provider `tools[]`；
- new/changed MCP 复用 Round 9 late META catalog；
- disconnected installed DIRECT descriptor 继续保留并由现有 unavailable gate 拒绝；
- Skill materialization变化只由 Round 9.1 产生 ordinary append-only catalog successor；
- Hook context 只追加来源无关的 `HOOK_CONTEXT` user-role observation；
- Subagent preset 只在新 child cold epoch安装；
- compaction successor 是合法 cold rebase：它重新读取current Plugin-derived Skill/MCP/Hook目录状态；已经admit的active child则继承其predecessor中已安装的exact preset snapshot，不用current package偷换启动说明；
- Hook definition refresh 只影响未来 event，不回写历史 Hook output。

### 0.5 Durability 与 oracle

本轮允许的跨进程状态只有：

- managed Plugin package copy；
- enabled user/workspace Plugin 配置；
- Plugin writable data directory；
- 已物化 Skill 的 owner provenance sidecar。

Plugin Hook的exact-definition trust digest由Round 9.2 generic `HookTrustStore`既有状态覆盖，不是本轮新增的Plugin状态。

它们是本地产品配置与安装状态，不是 conversation execution recovery。

本轮不新增：

- PostgreSQL relation、column、migration；
- committed/live event、subject slot、append guard；
- durable job、receipt、checkpoint、reducer、replay、repair graph；
- Hook execution history relation；
- durable Plugin capability graph；
- cross-Host Hook、MCP、Skill 或 worker execution recovery。

激活后的architecture oracle继承Round 9.2已经扩展的七维口径；Round 9.3不得再增加任何一维或扩大任何计数：

```text
Committed events   29
Live events        24
Subject slots      11
Append guards       1
Product relations  25
Durable jobs        0
Hook event types   11
```

固定顺序简写为`29 / 24 / 11 / 1 / 25 / 0 / 11`。最后一项是Round 9.2拥有的`HookEventType` vocabulary；Round 9.3只把Plugin声明映射到这11项既有类型，不新增第12项，也不建立Plugin-private Hook event enum。

---

## 1. 范围与明确非目标

### 1.1 本轮实现

1. Agent Plugins 1.0.0 root `plugin.json` parser；
2. Codex `.codex-plugin/plugin.json` primary host adapter；
3. Claude `.claude-plugin/plugin.json` secondary host adapter；
4. local path package add/replace/remove/enable/disable/list/doctor/gc；
5. user scope与exact workspace scope composition；
6. portable/Codex/Claude Skill 与 MCP normalization；
7. Codex/Claude package Hook declaration adapter，复用Round 9.2当前公开11-event command contract；
8. Claude `agents/*.md` 的安全最小子集与 Round 10 optional preset ref；
9. mid-session explicit `KernelHostCore.reload_plugins()`与固定ROOT `reload_plugins` Builtin；
10. strict-prefix、failure isolation、physical close与real-provider dogfood。

### 1.2 本轮明确不实现

- remote marketplace、Git/npm download、publisher signing、auto-update、dependency resolver；
- Codex Apps、`.app.json`、UI、assets renderer、marketplace metadata；
- Claude LSP、monitors、PATH injection、settings、themes、output styles、commands；
- 修改或复制Round 9.2已经实现的USER/WORKSPACE custom Hook config/runtime；managed Hook仍不支持；
- Hook `prompt`、`agent`、`http`、`mcp_tool` handler；
- Hook argument rewrite、ToolResult rewrite或permission policy mutation；
- Plugin Python entry point、dynamic import、shared-library injection；
- Plugin-defined permission preset、effect taxonomy、provider adapter或model target；
- Plugin-defined durable event/job/repository callback；
- subagent tool allowlist、model override、memory、worktree isolation、recursive worker或persistent named-agent session；
- 在 provider prompt 中自动注入完整 Plugin package catalog。

### 1.3 Hard-cut 删除的旧稿结构

以下旧设计不保留兼容 wrapper：

- Plugin Skill 作为第五个 `SAFE_POINT_REFRESHABLE` Skill root；
- `skill_root_registrations`；
- `<plugin-id>:<skill-name>` 的 Skill 重命名；
- dormant `agents/` inventory；
- `PluginHookActivationContextOwner`；
- `PluginHookDispatcher`与`PLUGIN_HOOK_CONTEXT`这类把通用Hook execution绑死到Plugin来源的命名/owner；
- Plugin-wide generation、package snapshot fingerprint、contribution-plan fingerprint；
- Hook registry fingerprint 与 `fingerprint -> object` map；
- “Round 5B/10 尚未实现”的 dormant producer；
- 64 enabled plugins、16 selected handlers 等无独立产品理由的总量 caps；
- per-file code/document/evidence SHA gates。

---

## 2. 公开标准与兼容优先级

### 2.1 Agent Plugins 1.0.0 是 portable truth

Portable package必须遵循 Published 1.0.0：

- root `plugin.json` 必须存在；
- `$schema` 为 `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`；
- portable component只有固定 `skills/` 与 `mcp.json`；
- `skills/`只把每个immediate child directory中exact regular `SKILL.md`识别为一项，invalid Skill逐项skip，不递归把更深后代猜成另一项；
- `mcp.json` 使用 `https://agent-plugins.org/schemas/1.0.0/mcp.schema.json`；
- portable manifest不能改写固定 component location；
- `plugin.json`的unknown top-level field按标准report-and-ignore；`extensions`不是object时只忽略该field；除此以外的schema violation拒绝整个portable package；
- 未实现的`extensions.<namespace>`整块忽略且不得深入验证；Pulsara不得把其中内容猜成Hook、agent或permission配置；
- Skill逐项失败、MCP server逐项失败，不牵连其他合法 component；
- 所有 package-relative path解析后必须仍位于 package root；
- client extension不得冒充 portable core。

Pulsara 支持 portable stdio 与 Streamable HTTP。Legacy SSE是标准允许但客户端可选的 transport；本轮若现有 MCP owner不能完整证明其redirect/origin safety，则对该 server给出 typed unsupported，不做 transport fallback。

### 2.2 Codex 是 primary host profile

Codex profile支持：

```text
.codex-plugin/plugin.json
skills/
.mcp.json
hooks/hooks.json
```

以及 Codex manifest中 `skills`、`mcpServers`、`hooks` 的合法 package-relative path声明。`hooks`可为：

- 一个path；
- path数组；
- inline hook object；
- inline object数组。

manifest指定`hooks`时覆盖默认`hooks/hooks.json`。`.app.json`与assets只报告 unsupported，不影响其他 component。

“Codex primary”表示优先兼容其公开package shape、Skill/MCP/Hook配置与当前11-event command lifecycle，不声称复刻Codex marketplace UI或Skill slash-command namespace。Pulsara不会为避免冲突改写标准Skill的frontmatter/name；两个Plugin携带同名Skill时按§4的typed collision与现有four-root precedence处理，而不是生成非标准alias。

这里也不声称把Codex Hook的authority逐字照搬：Pulsara按用户已冻结的边界把`additionalContext`降为`UNTRUSTED_OBSERVATION`，不实现argument rewrite，也不让PostToolUse替换canonical ToolResult；`PostToolUse.tool_response`只提供Round 7.1现有bounded public projection，不额外暴露raw oversized/private payload。Codex当前会把超大Hook output spill到temporary file并把path交给模型；Pulsara本轮只产生有界head/tail preview，不建立Hook专用artifact或read path。Pulsara `terminal`在yield时已经提交model-visible `RUNNING` ToolResult，因此对应PostToolUse发生在该result后；后续process completion不会假装成尚未交付的原始Bash result。CLI conformance/doctor必须把这些少数deliberate authority/payload/canonical-timing差异列出，不能用“Codex-compatible”掩盖。

### 2.3 Claude 是 secondary host profile

Claude profile支持与本轮重叠的：

```text
.claude-plugin/plugin.json
skills/
.mcp.json
hooks/hooks.json
agents/*.md
```

Claude manifest的`agents` additional paths同样属于本轮Agents兼容面：接受一个package-relative Markdown file/directory path或其数组，并与默认`agents/`共同组成preset discovery inputs。所有path必须resolve在managed package root内；directory按§7递归，single file只读取该exact `.md`。同一physical file经default与declared path重复命中时按canonical contained identity去重，不生成第二preset。

Claude 独有的 LSP、monitors、settings、commands 等不导入。Claude Hook只接纳本文与Codex当前公开契约一致的11项event集合及`type=command` handler；`SessionEnd`是两家当前契约的交集。其他event或handler在最窄单元上typed skip。

Skill只接受与Codex/Agent Skills共同的`skills/<name>/SKILL.md`目录形状；Claude独有的root-level single `SKILL.md`与legacy flat `commands/*.md`不进入本轮。它们由doctor typed报告而不是被误扫成portable Skill。

`agents/`是Claude extension，不是Agent Plugins portable core，也不是Codex Plugin component。Pulsara只支持§7定义的安全最小子集。

Claude允许只靠default component locations、没有`.claude-plugin/plugin.json`的目录。Pulsara V1不从directory basename猜canonical Plugin identity，因此这种manifest-less Claude package返回typed `UNSUPPORTED_MISSING_CANONICAL_PLUGIN_ID`；作者需提供Claude manifest、Codex manifest或portable root manifest后再安装。该限制必须由doctor显式报告，不能伪装成完整Claude package compatibility。

### 2.4 多 manifest package 的单一归一化规则

一个package可以同时携带portable、Codex、Claude manifest以服务不同host；Pulsara不因此拒绝跨客户端package。

Identity与component优先级固定为：

```text
identity metadata:
  root plugin.json
  > .codex-plugin/plugin.json
  > .claude-plugin/plugin.json

Skill/MCP:
  portable fixed locations when root plugin.json exists
  > Codex declarations/defaults
  > Claude declarations/defaults

Hooks:
  Codex declaration/default
  > Claude declaration/default

Subagent presets:
  Claude default agents/ + manifest-declared agents paths only
```

按上述优先级第一个合法manifest的name唯一拥有`plugin_id`。低优先级Codex/Claude manifest若声明不同name，只产生`SECONDARY_MANIFEST_NAME_IGNORED` diagnostic：它不能覆盖identity、创建第二Plugin instance或改变portable fixed Skill/MCP path，也不应让已经合法的portable core整包失效。被高优先级覆盖的component声明同样只产生diagnostic，不形成第二份贡献。Authoritative manifest自身name不合法仍按其公开schema拒绝对应package/profile；不得从低优先级name或directory basename回退猜identity。

### 2.5 既有生态探针为何只执行command Hook

起草期基于[Claude official marketplace固定提交](https://github.com/anthropics/claude-plugins-official/blob/49b5ab1a022e9f7daa72e35ec10bff3ee20a4a52/.claude-plugin/marketplace.json)的第三方Plugin窄探针仍作为兼容取舍依据：42份实际root Hook配置中，37份只使用本文11项兼容并集与command handler；把没有root Hook的external entries一并计入时，228/233的样本在静态Hook vocabulary/handler shape上不要求其余Claude-only执行能力。该数字不是完整兼容率，也不证明脚本stdin/env语义相同；它只说明“支持Codex当前公开的11项events与command handler，对prompt/agent/其余Claude-only event做最窄typed skip”能覆盖主流核心路径，而无需提前建设第二个模型调用型Hook engine。

双轨package证明作者会主动提供宿主适配；AWS/Expo/Semgrep等反例又证明common event、Claude-only handler/event可以混在同一文件。因此failure boundary必须停在exact event/group/handler，不能因一个unsupported sibling废掉整个Hook文件、Skill/MCP core或Plugin package。

---

## 3. Package parser 与本地生命周期

### 3.1 Pure package facts

```text
PluginScope
  USER
  WORKSPACE

PluginManifestProfile
  PORTABLE_V1
  CODEX
  CLAUDE

FrozenPluginPackage
  plugin_id
  package_install_id
  scope
  exact workspace identity | NONE
  managed package root
  writable data root
  authoritative metadata
  present manifest profiles
  Skill component description
  MCP component description
  Hook component description
  Subagent preset component description
  component diagnostics
```

它携带完整immutable值，不再添加`package_snapshot_fingerprint`。

`package_install_id`由每次成功`add`或`add --replace`生成并随local install state保存；replace即使manifest version与Hook command文本未变，也必须得到新identity。它只区分两次明确安装，不是mutable generation或package内容fingerprint。

`plugin_id`保留authoritative manifest schema定义的canonical logical name，不通过slug规则改写生态identity。凡是出现在local path中的`<plugin-id>`都必须使用一个reversible、single-component filesystem-safe storage encoding；logical id继续用于CLI、diagnostic、MCP namespace与preset scoped name。Host-profile name无法按其公开schema形成canonical identity时拒绝package，不得用basename或脆弱规则猜名。Composite MCP server id仍需通过existing 128-byte config validation；单个overbound entry typed unavailable，不影响同package其他component。

Package/Hook/MCP private bodies使用`repr=False`或等价closed carrier。CLI inspection、doctor、exception、diagnostic与dogfood只对exact active `PULSARA_API_KEY`值做固定替换，其他prompt、Hook input/output、model reply、DSN与配置内容保持可观察；不得为了“安全感”恢复广泛secret scanning/redaction。

### 3.2 Parser physical bounds

这些是单个不可信配置文档的parser/RSS边界，不是Plugin数量或长期历史上限：

```text
maximum one manifest or hook JSON bytes  1 MiB
maximum JSON/YAML nodes                 16,384
maximum JSON/YAML depth                     64
maximum one scalar UTF-8 bytes           64 KiB
maximum filesystem path UTF-8 bytes       4 KiB
maximum command UTF-8 bytes               8 KiB
maximum matcher UTF-8 bytes               1 KiB
maximum status message UTF-8 bytes         4 KiB
```

不得新增：

- total installed/enabled Plugin count；
- total package files/bytes；
- total Hook groups/handlers；
- total agent preset count；
- total Plugin history或lifetime。

Package copy必须streaming处理并依赖已验证的local filesystem/storage quota。落在source package root内的symlink可在防竞态重验后被dereference为managed root中的ordinary file/directory；dangling、escape、cycle、junction、device、socket、FIFO或复制期间identity变化拒绝最窄component/package path。Managed version root本身不保存指向source checkout的link。Manifest/component自身继续受各自parser与Round 9/9.1/MCP现有边界约束。

Copy必须保留portable bundled stdio executable所需的执行语义，但不能复制特权filesystem metadata。POSIX source regular file只保留“是否具有任一execute bit”这一事实：可执行文件在managed root归一化为owner-readable/writable且可执行的ordinary mode，非执行文件归一化为owner-readable/writable ordinary mode；directories归一化为owner可遍历模式。Setuid、setgid、sticky、ACL、ownership、xattr、resource fork与mtime都不进入产品语义，也不得复制成执行authority。Windows使用ordinary managed-file语义，由existing executable resolution在spawn前验证。这样`command: "./bin/server"`可继续运行，同时package不能借安装保留privileged bits。

若当前配置存在non-empty `PULSARA_API_KEY`，streaming copier必须用跨chunk boundary安全的exact-byte matcher检查将被写入managed root的每个regular file；每段bytes必须先与process-local carry共同检查、再写temporary file，不能先落完整secret后补扫。命中时删除temporary sibling并typed拒绝整个add/replace，diagnostic只报告path与`PULSARA_API_KEY_VALUE_PRESENT`，绝不能包含matched value。它只匹配该一个active secret的exact bytes，不扫描、猜测或脱敏其他内容；环境变量名文本`PULSARA_API_KEY`本身不是secret value。这样Plugin Skill/resource/Hook config不会借package copy把该值复制进managed store。

### 3.3 Managed store

Package与state存入Pulsara local home，不让repository内容自动启用可执行Hook：

```text
${PULSARA_HOME}/plugins/packages/<scope-key>/<plugin-id>/<package-install-id>/
${PULSARA_HOME}/plugins/data/<scope-key>/<plugin-id>/
${PULSARA_HOME}/plugins/state/<scope-key>/<plugin-id>.json
${PULSARA_HOME}/plugins/locks/<scope-key>/<plugin-id>.lock
```

`scope-key`使用existing canonical workspace identity的filesystem-safe stable representation；`plugin-id` path segment使用§3.1的reversible storage encoding，不得重新发明workspace root算法或直接把逻辑name拼进path。

不存在全局`state.json`或全局store mutex。每个Plugin instance的state只在自己的`scope-key + plugin-id`分片内原子replace；单个分片复用§3.2的1 MiB配置文档物理上界。Host composition按确定顺序枚举USER与exact WORKSPACE state目录并逐个读取完整分片，因此安装数量不需要总cap，也不会让一次无关Plugin更新重写不断增长的全局JSON。Hook trust完全归Round 9.2 generic `HookTrustStore`，不在Plugin store复制第二份状态。

两个分片的closed product state固定为：

```text
PluginInstanceState
  contract_id
  logical plugin_id
  scope = USER | WORKSPACE
  exact canonical workspace identity | NONE
  current package_install_id
  enabled: bool
```

它不复制manifest、component facts、package tree hash或Runtime view。`add`创建新的immutable root与`enabled=false` state；`enable/disable`只切换同一current install的enabled值；`add --replace`原子切换current install并保留原enabled值。New install id进入Round 9.2 Plugin Hook source identity与trust digest，因此旧trust自动成为MODIFIED而不能授权new package。`remove`只删除instance state；generic orphan trust可以由`pulsara hooks doctor`显示并由用户revoke/清理，但永远不能在缺少current exact Plugin source时执行。Package lifecycle的linearization是state replace/delete；Hook trust/revoke的linearization属于Round 9.2 `HookTrustStore`，二者不伪造跨文件事务。

一次composition refresh的state观察规则必须closed但不伪造filesystem transaction：每个scope的成员集合以该次bounded directory enumeration为准，枚举完成后新增的instance进入下一次refresh；每个已枚举分片只接受一次成功读取的exact regular-file bytes。已枚举分片在读取前消失、identity变化、无法形成完整bounded bytes，或引用的immutable package root无法取得borrow时，本次refresh整体形成`PLUGIN_STATE_DISCOVERY_RACED/UNAVAILABLE`，不得用读到的一半发布新的package tuple，也不得循环重试到新的deadline。Running Host保留predecessor component views；cold Host不执行未证明完整的Plugin MCP/Hook/preset贡献，Plugin-managed Skill则由§3.5 pre-scan reconciliation令聚合catalog `UNAVAILABLE`。这只定义一次process-local observation cut，不增加global generation、inventory fingerprint或durable coordinator。

所有会修改某一instance的package versions、state、Skill materialized projection或GC结果的操作只取得该instance的local lock，再执行read-modify-atomic-replace。Generic Hook trust命令使用Round 9.2自己的per-source lock，不取得Plugin state lock；写前重新读取current Plugin source identity/digest，因而不会把stale inspection授权给replacement。纯读取MCP/Hook/preset composition的Host可以读取atomic state snapshot而不持锁；Host pre-scan Skill reconciler一旦需要create/replace/remove projection，必须取得同一instance lock、在锁内重新读取current state/install id/provenance并证明仍等于本次观察，随后才atomic发布。若重读已经变化，本次reconciliation形成`DISCOVERY_RACED`并留给下一cut，不能用旧观察覆盖new state。跨多个instance的`list/doctor/gc/reconcile`逐项处理并允许调用方取消，不承诺虚假的all-Plugin transaction。该filesystem mutex只防止同一instance的local state/projection丢失更新，不是durable execution lease、package generation或跨Host协调表。

`add`：

1. resolve exact local source；
2. streaming复制到temporary sibling；
3. validate manifest、path containment与component descriptions；
4. fsync必要文件；
5. 生成新的`package_install_id`并atomic rename为该immutable version root；
6. state只引用exact install id；
7. 不自动enable，不自动trust Hooks。

`add --replace`是唯一update路径：先安装一个新的immutable version root，再atomic切换state中的current install id；绝不原地改写旧root。这里的多个physical version不是old/new产品兼容或capability generation，而是为了保证running Host冻结的Hook command、MCP command path与trust identity不会在脚下被替换。

每个Host读取一个package version时取得private `PluginPackageBorrow`：它持有exact install id/root与一个OS-released-on-process-exit shared lock。Borrow必须由实际仍依赖package bytes或executable path的consumer持有，不能把“本Host曾加载过”当成保留到Host close的理由：Skill在atomic materialization完成后释放；preset directory冻结完整definition/body后释放，accepted task只持有其process-local frozen body；Hook predecessor view在已dispatch attempts全部settle后释放；MCP config/slot在old server process、pending connect与remote invocation全部drain后释放。多个consumer可以共享同一个process-local borrow owner，但其唯一职责是last-consumer close；不得演化成version registry、durable lease、borrow history或`borrow -> object` map。Host close/crash仍兜底释放全部未settle handle。该borrow不进入DTO fingerprint、provider输入、repository或recovery状态。

`remove`先在exact instance lock内删除其state并使future Host不可见；package version目录只做opportunistic GC。`pulsara plugins gc`或store启动清理逐个instance取得该lock，再只在取得version lock的nonblocking exclusive ownership后删除未被current state引用的root；有running Host borrow时跳过，不能阻塞或中断Host。Host crash后OS释放lock。不存在version数量cap、定时repair job或durable lease table；磁盘使用继续受已验证local storage quota约束。

`remove`默认保留独立writable data root，避免删除Plugin产生的用户数据，也避免与尚未reload的Host竞争。V1不提供自动data GC；用户显式清理该目录属于普通filesystem操作，不是Plugin execution recovery。

仓库仍处开发阶段，不为旧的原地单目录layout保留dual-read、legacy alias或迁移wrapper；verified local development state直接hard-cut/reset。

### 3.4 Enablement 与 scope

状态文件原子replace，表达：

```text
USER plugin instance
  visible to all workspaces in this Pulsara home

WORKSPACE plugin instance
  visible only to exact canonical workspace identity
```

同一`plugin_id`在exact workspace同时存在USER和WORKSPACE实例时，两者都保持独立package instance；不得声称WORKSPACE可以“整包shadow”已经物化到user Skill root的内容。有效贡献按existing owner能够机械证明的leaf identity决定：

- Skill继续使用Round 9.1的root precedence与exact Skill name；workspace同名Skill胜出，user-only Skill仍可见；
- MCP按`plugin-id + package-local-server-id`取workspace entry；user instance中没有发生identity冲突的server仍可见；
- Hook为避免同一Plugin两份lifecycle policy叠加，workspace Hook component存在时替代该Plugin的user Hook component；workspace package没有Hook component时user Hook仍可见；
- Subagent preset按`plugin-id + relative namespace + agent-name`取workspace entry；user-only preset仍可见。

该规则分别在Skill/MCP/Hook/preset owner输入冻结前执行，不建立跨ownerwinner transaction。CLI/doctor必须显示两个package instance及每个component/leaf的effective或shadowed disposition。

Package不会因存在于workspace checkout、marketplace目录或`.pulsara/`下就自动enable。Enable是明确用户操作。CLI enable前必须显示current package的normalized Skill/MCP/Hook/preset component summary，尤其是每个stdio MCP command/cwd/env keys与HTTP endpoint。Enable Plugin MCP即授权existing supervisor在合法composition时以当前Host OS用户启动stdio server或连接HTTP endpoint；这项physical server startup/connect发生在具体MCP tool permission之前，不能被描述成“只有模型调用工具才会执行外部代码”。每次remote tool invocation仍必须经过Round 6/9的scope、permission、effect、dirty与attempt owner，Plugin enable不授予调用bypass。

Enable也不等于Hook trust：

- Skill仍是untrusted data；
- MCP仍经过现有permission/effect policy；
- Hook必须exact trust；
- Subagent preset仍是untrusted startup guidance。

### 3.5 Running Host refresh

Host提供：

```text
KernelHostCore.reload_plugins()
```

并从Round 9.3 hard-cut后的每个cold Host开始固定暴露：

```text
reload_plugins()
```

该Builtin是ROOT-only、execution-backed、非read-only的process-local composition action；descriptor始终存在以保持same-epoch tools稳定，只有`BYPASS_PERMISSIONS`可以成功。它不执行`add/enable/disable/trust`，不写Plugin state，也不把模型输入变成package authority；它只在safe point重读已经由CLI原子提交的current state并返回各component的published/unavailable/unchanged disposition。这样模型可先通过已授权Terminal运行local lifecycle CLI，再显式要求当前Host采用变化，而无需filesystem watcher或重启。

它是explicit process-local refresh，不是filesystem watcher、durable generation或recovery owner。Plugin state是enablement truth；四根目录中的Plugin-managed Skill只是可重建的filesystem projection。所有lifecycle command，以及running/new Host的**每一次ordinary complete Skill scan**，都必须先由Host composition layer按current state和exact provenance执行一次bounded synchronous Plugin Skill reconciliation；随后才调用不感知Plugin的existing `LocalSkillProvider`。这次pre-scan reconciliation只维护Skill filesystem projection，不顺带refresh MCP、Hook或preset view，并与本次dispatch planning共享同一个absolute deadline。无法证明reconciliation complete时，本次聚合`LOCAL_SKILL_CATALOG`形成typed `UNAVAILABLE/PLUGIN_RECONCILIATION_FAILED`，不能让stale Plugin目录作为普通Skill重新进入catalog；它不阻断conversation，也不启动后台repair job。

Reconciliation的输入不是“当前仍存在的state files”单边集合，而是两个完整、bounded、可取消的枚举结果之并集：current USER/exact WORKSPACE Plugin instance states，以及对应user/workspace target roots中所有携带Plugin owner provenance sidecar的Skill directories。后者让已经删除state但CLI在projection cleanup前崩溃的orphan目录仍能被发现。对每个instance取得§3.3 exact lock并重读current truth后：enabled current contribution应存在且逐项匹配；disabled/missing/replaced contribution在content digest仍匹配时删除；用户已修改时只删除Plugin provenance并保留为ordinary unmanaged Skill。任一目标root无法完整枚举、已枚举sidecar/目录发生identity race或lock内重读无法与本次cut闭合，都使本次reconciliation `UNAVAILABLE`，不得用只看state或只看目录的partial winner继续scan。

`enable/disable/add --replace/remove`都在exact instance lock内以该instance state分片的atomic replace/delete作为package lifecycle唯一产品linearization point。State提交后，同一CLI可继续在同一lock ownership内尽力完成Skill projection reconciliation与必要diagnostic；某个component失败不回滚已经成立的enablement truth，也不假装其他owner已原子切换。Future或running Host都会在下一次complete Skill scan前按上段规则取得exact instance lock并再次同步reconcile，所以CLI进程在state提交后崩溃只会留下可诊断、可由下一合法scan重建的projection drift，不需要repair job。Disable/remove必须先使state不可见，再删除仍与provenance exact匹配的Skill目录；若进程在两步之间中断，pre-scan reconciliation必须先完成清理或令该次聚合catalog `UNAVAILABLE`，不得把未启用文件误当成authority。

外部CLI修改state后：

- future Host直接读取新state；
- 成功完成Skill materialization/removal的变化可由running Host在下一次ordinary complete Skill scan中自然发现，不等待Plugin reload；
- 同一installed epoch中的MCP、Hook与preset只有收到explicit reload control才hot-refresh；
- ordinary cold open与Round 5B compaction successor在冻结current owner snapshots/capability cut前自动调用同一个composition refresh，因为它们本来就是合法cold reconstruction boundary；
- external embedder可直接调用Host method；对话内模型或用户通过固定`reload_plugins` Builtin触发同一实现；若两者都不可用才需要restart Host。

因此改变Plugin enablement/current install的CLI必须明确返回`running_host_reload_required`及调用`reload_plugins`/关闭Host的提示；generic Hook trust/revoke命令则提示调用Round 9.2 fixed `reload_hooks`。Disable/remove只在Plugin state linearization point阻止future composition，Hook revoke只在generic trust state linearization point阻止future trust join；一个已经持有old immutable definition/borrow的running Host在对应reload或close前仍可能执行old Hook/MCP。CLI不得谎称已经同步撤销所有进程内执行能力，Runtime也不得用filesystem watcher偷换view。

Refresh在Host safe point先为新的exact enabled package tuple取得immutable package borrows，再把贡献分别交给Skill、MCP、Hook与preset owner。不同physical owner没有虚假的跨owner事务瞬间；component各自在自己的合法linearization point生效。失败的refresh释放本次尚未发布的新borrow；成功replacement的predecessor borrow只保留到§3.3列出的old consumer全部drain，不能为了实现方便积累到Host close。

Compaction的顺序固定为：install exact-scope fence -> dispatch `PreCompact` against predecessor Hook view -> 若未abort则执行Plugin composition refresh -> freeze successor Plugin/capability/cold inputs -> ordinary summary/adoption。Refresh发布的新Hook view可以观察后续`PostCompact`与`SessionStart(compact)`，但不能倒流重跑本次PreCompact。Refresh与后续summary任一失败都不伪造跨ownerrollback：已经在各owner合法发布的Plugin component保持current，并继续遵守old epoch tools不变/late-meta消息追加规则。

`reload_plugins`这次Tool invocation的Pre/Permission/Post Hook必须全部使用attempt开始时冻结的predecessor Hook view；本次reload新发布的Hook定义只能从后续lifecycle event生效，不能在自己的PostToolUse阶段自触发。Reload ToolResult按Round 7.1 normal projection提交，不形成Plugin receipt或durable generation。

---

## 4. Skill contribution：只进入现有四根目录

### 4.1 禁止第五个 root

Round 9.1唯一合法Skill roots保持：

```text
workspace/.pulsara/skills
workspace/.agents/skills
${PULSARA_HOME}/skills
~/.agents/skills
```

Plugin不得注册自己的root、cache root或source kind。它必须把portable Skill目录**原样物化**到：

```text
USER Plugin      -> ${PULSARA_HOME}/skills/<skill-name>/
WORKSPACE Plugin -> <workspace>/.pulsara/skills/<skill-name>/
```

`.claude/skills`仍不扫描。Plugin也不得把Skill改名为`<plugin-id>:<skill-name>`；Agent Skills要求frontmatter name与目录basename一致，改名会改写外部标准内容。

### 4.2 Materialization 与 provenance

每个目标Skill目录额外带existing `.pulsara-skill-source.json` provenance。Plugin-managed variant至少记录：

- owner kind=`plugin`；
- plugin instance与package install identity；
- source package-relative path；
- installed source-content digest。

该digest直接复用现有`compute_skill_dir_hash()`内容契约：覆盖ordered relative file path与bytes，排除`.pulsara-skill-source.json`自身以及该helper既有的cache/OS ignored files，避免自引用或发明第二种Skill tree hash。Plugin source tree若自己携带reserved `.pulsara-skill-source.json`，该exact Skill contribution拒绝，不能让包伪造owner sidecar。该digest跨filesystem/restart边界证明“这个managed目录的产品内容仍是Plugin上次物化的内容”，是允许保留的immutable content digest。

安装/update/disable规则：

1. destination不存在：staging copy + atomic directory rename；
2. destination由same Plugin instance管理且content仍匹配：允许atomic replace；
3. destination属于其他Plugin、bundled Skill或普通用户Skill：该Skill contribution typed collision，绝不覆盖；
4. Plugin-managed destination已被用户修改：不覆盖、不删除用户内容；删除Plugin provenance使它成为普通unmanaged Skill，并报告ownership conflict；
5. disable/remove只删除仍与provenance digest exact匹配的目录。

一个Skill collision不阻止同package的其他合法Skill、MCP或Hooks。

### 4.3 Discovery 与 continuity

物化完成后不直接构造catalog。现有LocalSkillProvider在下一次ordinary complete scan中决定：

- four-root precedence；
- manifest validity；
- COMPLETE或UNAVAILABLE；
- ROOT/child scope；
- current catalog successor。

Plugin materialization使用atomic directory rename；并发scan若观察到已枚举文件消失或读取不完整，继续按Round 9.1形成`UNAVAILABLE/DISCOVERY_RACED`，下一cut重试。Plugin不得添加partial catalog、loaded-state或repair job。

---

## 5. MCP contribution：进入现有唯一 config inventory

### 5.1 Normalization

MCP document envelope先按profile closed解析，不能把三家的top-level shape互相猜测：portable `mcp.json`严格接受Agent Plugins 1.0的`$schema + mcpServers`；Codex `.mcp.json`按公开契约接受direct server map或wrapped `mcp_servers` object；Claude default `.mcp.json`接受direct server map或wrapped `mcpServers` object。Claude manifest inline/path-array `mcpServers`、`.mcpb`与Codex `.app.json`不属于本轮执行面，逐component typed unsupported，不能回退到另一profile parser。Top-level envelope损坏只使该MCP component unavailable；合法envelope中的individual server继续按对应profile的最窄entry boundary校验。

每个enabled Plugin MCP entry归一化为existing `McpServerConfig`。Runtime server id为：

```text
<plugin-id>.<package-local-server-id>
```

这只namespaces Host config identity；remote MCP tool name、schema与policy仍由Round 6/9 owner定义。

Plugin package不是Host startup policy authority。所有Plugin-derived server固定normalize为`required=false`，单个start/connect/auth/handshake failure只使该server unavailable并保留其他server/component；Plugin manifest不能把它提升成required Host dependency。Visibility固定为existing `ROOT_AND_SUBAGENTS`并继续受exact Plugin USER/WORKSPACE scope约束；effect保持existing `AUTO` inference/override owner，package不能声明permission bypass、effect override或parallelism authority。Codex/Claude profile中超出其公开MCP config交集的host-policy字段typed unsupported，而不是翻译成Pulsara authority。

这里的`required=false`只保证server failure不阻塞Host，不把server启动变成无副作用操作。Enable/inspect输出必须明确展示stdio code execution或HTTP connection boundary；Plugin MCP process继承的environment继续由existing transport policy与本文secret删减规则决定，不能把Tool permission误当成server-process sandbox。

Portable stdio必须保留标准的字段级语义：

- `command`是一个bare executable token或以`./`开头的package-relative token；不得做placeholder expansion或shell split；后者在adapter中resolve为managed package内的exact executable path；
- omitted `cwd`使用managed package root；显式`cwd`只接受`./...`、`${PLUGIN_ROOT}`或`${PLUGIN_DATA}` anchored forms，展开后必须留在对应root；
- 只在`args`每个string、`env`每个value与`cwd`中对`${PLUGIN_ROOT}`/`${PLUGIN_DATA}`做一次non-recursive textual replacement；replacement产生的新`${...}`不得再次扫描，unknown placeholder-like text保持literal而不是读取Host secret；env key、command与fixed component path都不展开；
- portable `env`声明`PLUGIN_ROOT`或`PLUGIN_DATA`会使该server entry invalid；configured env overlay之后由client最后写入这两个真实值，并删除任何名为`PULSARA_API_KEY`的entry；任何Plugin-derived stdio env、secret-ref、HTTP header env-ref或placeholder试图读取/声明`PULSARA_API_KEY`时，该MCP entry typed rejected，不能通过另一种transport/config shape绕过；
- adapter通过sealed factory给existing stdio transport增加一个already-validated exact local cwd branch；ordinary config继续使用workspace-relative branch。MCP physical owner只消费closed value并在spawn前重验目录仍存在，不导入Plugin parser或package store。

Portable Streamable HTTP必须冻结literal public headers，而不是把它们误写成environment-secret references。Round 9.3给existing auth/config union增加`StaticLiteralHeaders` pure value；URL拒绝userinfo/fragment，non-loopback endpoint必须HTTPS，HTTP只允许host恰为`localhost`或由`ipaddress`机械证明的任意loopback IP literal。为符合portable 1.0，existing URL validator的loopback判断从当前三个literal扩展为closed `localhost | ipaddress.is_loopback`，但不通过DNS把普通hostname猜成loopback。Private non-loopback HTTPS仍受existing network policy；package本身不能放大该policy，policy拒绝是typed connection unavailable而不是伪造invalid portable schema。Literal headers不得展开环境变量或`${PLUGIN_*}`，也不得覆盖client-owned MCP protocol/auth headers。因为existing transport禁用redirect，它不会把header转发到另一origin。Portable OAuth不存在；legacy SSE在current owner没有transport实现时对该entry typed unsupported，不做fallback。

上述扩展只补齐existing `McpServerConfig`表达能力，不创建Plugin MCP transport、slot、registry或executor。Hook command环境同样提供`${PLUGIN_ROOT}`/`${PLUGIN_DATA}`，但两者不是共享execution owner。

### 5.2 Single composition input

```text
existing configured MCP servers
+ enabled Plugin normalized MCP servers
-> one ordered tuple[McpServerConfig, ...]
-> KernelHostCore.reload_mcp_configs(...)
```

server id冲突在reload前按entry拒绝；不得让Plugin覆盖base config或另一个Plugin。Plugin不创建MCP registry、slot、generation、executor、permission matrix或retry loop。

最终ordered tuple仍受existing `MAXIMUM_MCP_CONFIGURED_SERVERS`这一已激活physical owner bound约束；它不是Plugin数量cap。若current base + Plugin tuple整体越界，MCP component的本次reload typed rejected且supervisor继续使用old exact config tuple，不挑选partial Plugin winner；Skill、Hook与preset component可在各自linearization point继续生效。CLI/doctor必须报告current count、existing bound及本次MCP component未发布的事实。

### 5.3 Mid-session behavior

- late READY/new schema走existing `MCP_CATALOG` successor与`inspect_new_mcp_tool -> use_new_mcp_tool`；
- current epoch已DIRECT的same identity schema replacement继续按Round 9 unavailable/pending-cold-adoption规则，不能meta绕过；
- runtime-only reconnect不产生provider-visiblecatalog变化；
- disable/remove从current config inventory移除server，未来meta route消失；
- existing epoch的native descriptor不热删，调用由existing unavailable gate拒绝；
- next ordinary cold open或compaction successor重新决定完整MCP cohort的DIRECT/META exposure。

---

## 6. Hook contribution adapter

Round 9.3不实现Hook subsystem。11项event、USER/WORKSPACE source、trust store、matcher、dispatcher、executor、output parser、lifecycle seam与`HOOK_CONTEXT`全部由已ACTIVATED的[Round 9.2](ROUND_9_2_HOOK_SUBSYSTEM_IMPLEMENTATION_SPEC.zh.md)唯一拥有。

本节只冻结Plugin package如何成为Round 9.2的第三种definition producer。

### 6.1 Package declaration discovery

Codex profile接受：

- manifest指定的exact Hook file/path；
- manifest inline Hook object；
- 未指定时default `hooks/hooks.json`。

Claude profile接受其manifest公开的Hook path/array/inline forms及default `hooks/hooks.json`。Portable Agent Plugins 1.0没有Hook component；不得从`extensions`猜测Hook。

Adapter只负责定位并取得完整bounded bytes，再调用Round 9.2唯一`hooks/parser.py`。它不得复制JSON schema、matcher normalization或output parser。Invalid/unsupported event/group/handler使用Round 9.2既有最窄failure boundary；Plugin的Skill/MCP/preset component不受无关Hook entry牵连。

### 6.2 Plugin Hook source identity

Round 9.3以一次明确hard cut给Round 9.2的closed source union增加：

```text
HookSourceKind.PLUGIN

PluginHookSourceIdentity
  plugin logical id
  visibility scope = USER | exact WORKSPACE
  exact package_install_id
  selected package-relative Hook config identity

PluginHookTrustSubject
  plugin logical id
  visibility scope = USER | exact WORKSPACE
```

这不是capability leaf、registry generation或新的Hook owner。Generic `HookSourceIdentity`继续由private factory构造；Plugin adapter不能把任意path或caller自报identity塞进dispatcher。

Current Hook source order扩展为：

```text
USER hooks.json
WORKSPACE hooks.json
effective Plugin sources in deterministic package/component order
```

USER/WORKSPACE custom Hook与Plugin Hook是append关系，不做semantic dedup。对于同一`plugin_id`的USER/WORKSPACE package instance，§3.4既有component winner先选出一个effective Hook contribution，再把它作为一个Plugin source交给generic Hook owner。

### 6.3 Provenance与environment overlay

每个normalized Plugin definition携带普通source provenance与closed environment overlay：

```text
PLUGIN_ROOT
PLUGIN_DATA
CLAUDE_PLUGIN_ROOT = PLUGIN_ROOT
CLAUDE_PLUGIN_DATA = PLUGIN_DATA
CLAUDE_PROJECT_DIR = exact workspace root
```

Package adapter冻结这些exact managed paths；Round 9.2 executor只消费values，不解析Plugin manifest/store。Plugin不能覆盖`PULSARA_HOOK_SOURCE_DIR`、`PULSARA_PROJECT_DIR`或Host-owned环境字段，不能声明`PULSARA_API_KEY`。

Command中的`${PLUGIN_ROOT}`/`${PLUGIN_DATA}`按host profile的公开契约由adapter执行单次、non-recursive substitution；unknown placeholder、cycle或escape使exact handler unavailable。Generic Hook core不承担Plugin placeholder expansion。

### 6.4 Generic trust reuse

Enable Plugin不trust Hook。Plugin source复用Round 9.2唯一`HookTrustStore`与CLI：

```text
generic state:
  ${PULSARA_HOME}/hooks/trust/plugin/<scope-key>/<encoded-plugin-id>.json
  ${PULSARA_HOME}/hooks/locks/plugin/<scope-key>/<encoded-plugin-id>.lock

pulsara hooks inspect --source plugin:<plugin-id> --scope ...
pulsara hooks trust --source plugin:<plugin-id> --scope ... --expected-definition-digest <digest>
pulsara hooks revoke --source plugin:<plugin-id> --scope ...
```

Generic trust state按稳定`PluginHookTrustSubject`定位，digest则在Round 9.2字段基础上覆盖current Plugin source identity、package install id、config identity与environment overlay。Package replacement即使manifest version/command文本相同，也会在同一trust subject下因new install id形成MODIFIED/UNTRUSTED，而不是悄悄成为另一个不相干的state key。不存在`PluginHookTrustState`、`plugins/hook-trust`、Plugin-private trust map或兼容alias CLI。

Plugin package lifecycle可以在enable前展示normalized Hook summary，但不能替generic `hooks inspect/trust`签发执行trust。Round 9.2 trust state是唯一truth。

### 6.5 Refresh与consumer lifetime

Cold Host/compaction successor在ordinary Hook source discovery中加入current enabled Plugin contributions。Running Host只有explicit `reload_plugins`才重新读取Plugin store/config、冻结Plugin source tuple并交给`KernelHookDispatcher`的existing future-view replacement seam。Round 9.3扩展generic `reload_hooks`：它可以对**已经安装在current dispatcher view中的Plugin source values**重读generic trust state并改变runnable disposition，但不重新读取Plugin store、manifest、package config或install identity。

`reload_plugins`自身的Pre/Permission/Post Hook使用attempt开始时的predecessor view；new Plugin definitions从下一event生效。Disable/remove/replace/revoke后，state/trust变更只阻止future composition；already-dispatched Hook attempts持有old immutable definition和package borrow直到settle。

Package borrow只保留到依赖该root executable/path的old Hook attempts全部settle；不能因Host曾加载该Plugin就留到Host close。Generic dispatcher不拥有package GC或version history。

### 6.6 No duplicated Hook machinery

Round 9.3 production必须满足：

```text
plugins/hook_adapter.py
  -> hooks/contracts.py
  -> hooks/parser.py
  -> existing HookTrustStore / KernelHookDispatcher

hooks/*  -X->  plugins/*
```

Plugin adapter不得拥有：

- `HookEventType`副本；
- matcher/compiler；
- process scheduler/executor；
- Hook output parser/control aggregator；
- pending context buffer；
- `HOOK_CONTEXT` renderer；
- lifecycle seam；
- trust persistence；
- Hook registry fingerprint/map。
## 7. Claude-compatible Subagent preset

### 7.1 为什么现在必须有真实consumer

Round 10已经ACTIVATED。旧稿继续保存dormant `agents/` inventory只会制造无人消费的DTO。本轮要么诚实不支持agents，要么接入现有task graph；本文选择最小接入，且不增加第二套agent runtime。

### 7.2 Accepted safe subset

Claude package默认`agents/`与manifest-declared `agents` directory按Claude Plugin现有语义递归枚举regular `*.md`；declared single-file path读取该exact regular `.md`。Directory-relative subdirectory segments构成preset namespace，不能被丢弃或扁平化；single-file source没有额外namespace。每个去重后的文件可形成preset。Required：

```yaml
---
name: code-reviewer
description: Reviews code for concrete defects.
---

Detailed role guidance...
```

Pulsara V1只执行：

- `name`；
- `description`；
- Markdown body。

`color`可作为inert display metadata被解析并原样用于目录显示，但不进入child prompt、task identity、permission、model或tool选择。除`color`外，未知frontmatter key不被猜测；它使exact preset形成typed unavailable diagnostic，等待未来规格显式定义其语义。

以下字段一旦存在，exact preset标为`UNAVAILABLE_UNSUPPORTED_SEMANTICS`，不得静默忽略后以更大authority运行：

```text
tools
disallowedTools
model (except omitted or inherit)
permissionMode
mcpServers
hooks
maxTurns
skills
memory
background
effort
isolation
initialPrompt
```

理由：Round 10固定所有worker都是不可递归leaf，继承current child capability/permission，并刻意不设置turn/lifetime总cap。忽略Claude限制字段可能把作者期望的read-only agent变成BYPASS/full-tool worker；临场翻译vendor tool名则会恢复脆弱metadata authority。

Preset Markdown文件复用existing `maximum_single_source_variant_bytes`物理边界；解析器不得再发明更小的Plugin专用正文cap。超过existing single-source bound、无法形成完整UTF-8 bytes或最终`PLUGIN_AGENT_CONTEXT FULL`不能通过child cold preflight时，exact preset typed unavailable，worker task不被接受。

Recursive discovery使用确定性、contained的ordered filesystem walk：default source先于manifest declaration order，同一source内按relative path排序；只接纳managed package root内的regular `.md` bytes，不跟随scanner阶段的新symlink，不把非Markdown supporting file猜成preset。Scoped identity固定为`<plugin-id>:<namespace-segment>...:<agent-name>`；namespace segment来自exact relative directory name，logical name冲突使该exact preset typed unavailable，不能靠遍历顺序挑winner。单个完整读取但frontmatter无效的文件只让该exact preset unavailable；任一required declared source不存在、目录无法完整枚举、已枚举文件在读取前消失/变化或无法形成exact bounded bytes时，整个package preset component `UNAVAILABLE/DISCOVERY_RACED`，不能把unknown remainder当成不存在后发布partial directory。这里不设置preset数量、目录深度或总文件历史cap；仍受§3.2 path/scalar、单文件source与本次可取消filesystem operation的既有物理边界。

### 7.3 Directory与opaque ref

新增固定Builtin：

```text
list_agent_presets(
  cursor?: string,
  limit: integer = 50  # 1..200
)
```

它返回current exact scope中可用preset的：

- scoped name `<plugin-id>:<namespace-segment>...:<agent-name>`；
- description；
- opaque `agent_preset_ref`；
- component diagnostics/pagination。

它只读current process-local Plugin composition，不连接、不reload、不启动worker。

`limit`复用现有local directory tool的单页边界，只限制一次provider结果而非preset总量。Page builder按deterministic preset order加入完整行，并在Round 7.1 logical 40,000-byte envelope前停止；`next_cursor`指向第一个未交付row。单个preset的完整`scoped name + description + opaque ref`无法形成FULL row时，该preset typed unavailable，description不得截短后继续路由。Directory ToolResult标记existing `FULL_REQUIRED/DIRECTORY_PAGE`；aggregate budget不fit时provider open为0，不得让模型使用未完整交付的ref或推进cursor。

Cursor使用process-local keyed opaque token，claims只含exact scope、current ordered preset-directory content commitment与next offset。该commitment用domain-versioned canonical framing覆盖ordered `(scoped name, package install id, preset definition content digest)`；它不覆盖physical borrow/diagnostic。MAC key与preset ref共用同一Host opaque-token issuer，Host restart或Plugin refresh后旧cursor typed stale。这里的commitment只服务跨model-call分页exact join，不进入registry、DTO proof或`fingerprint -> object` map。

Round 10的`spawn_agent`与`create_agent_tasks.tasks[]`增加optional：

```text
agent_preset_ref: string
```

ref是keyed MAC的closed claims：

- exact user/workspace scope；
- plugin instance与package install identity；
- scoped preset name；
- preset definition content digest；
- ref contract version。

这是跨model-call opaque token authentication的真实边界。Runtime不维护`ref -> object` map；use时从current preset view exact resolve并重验definition digest。Plugin disable/change使旧ref typed stale。

MAC key只由current Host process-local opaque-token issuer持有，不写state/database，也不复用`PULSARA_API_KEY`。Host restart后旧ref自然invalid，模型需要再次调用`list_agent_presets`；已经admit的nonterminal worker本来也按Round 10在Host loss后`INTERRUPTED`，因此不需要durable ref恢复。

`list_agent_presets`是ROOT-only fixed Builtin directory tool；它在四种ROOT permission mode中保持同一descriptor并可执行只读local lookup。真正创建task的Round 10 tools仍只在`BYPASS_PERMISSIONS`成功。Child surface不暴露该目录工具。

### 7.4 Round 10 integration

有preset时：

- built-in `profile`必须省略或为`general_worker`；
- task/dependency/result/status/recovery语义完全不变；
- canonical task row继续使用existing `general_worker`与`display_role`；
- `display_role`由scoped preset name确定；携带preset ref时调用方不得另传相互竞争的`display_role`；
- exact preset body只保存在Round 10 process-local start material；
- Host loss后nonterminal task仍按Round 10 `INTERRUPTED`，不恢复preset body；
- child cold assembly新增一个`PLUGIN_AGENT_CONTEXT` source。

```text
source      PLUGIN_AGENT_CONTEXT
contract    pulsara.plugin-agent-context.v1
trust       UNTRUSTED_OBSERVATION
lifecycle   ACTIVATION_SNAPSHOT
presence    VALUE
budget      MUST_KEEP
placement   44   # after PARENT_CONTEXT(42)/DEPENDENCY_RESULTS(43), before Skill catalog
variant     FULL only
body        scoped name + description + exact Markdown body
```

没有preset的ordinary ROOT/child cold input必须为该kind提供`NOT_APPLICABLE` absent fact；不得用空VALUE伪造已选择preset。

该source与objective、optional `PARENT_CONTEXT`、`DEPENDENCY_RESULTS`一起进入existing `SubagentInitialSeed`和唯一`KernelColdEpochInputAssembler`。它不能：

- 修改BASE_SYSTEM；
- 改变child provider tools、permission、MCP/Skill catalog或model target；
- 覆盖ROOT objective；
- 让child创建child；
- 建立loaded-agent registry或durable agent session。

Stable BASE_SYSTEM只增加通用读规则：Plugin agent context是用户安装的untrusted role guidance；system policy、ROOT objective、current permission与workspace truth优先。

Task admission一旦成功，就把exact preset body冻结进existing process-local start material。随后Plugin disable/replace不得偷换一个已经accepted但仍在`PENDING_START/WAITING_DEPENDENCY`的task；该task仍使用admission时的body。Host loss依旧按Round 10中断nonterminal task，不跨Host恢复该body。

Active child发生Round 5B compaction时，successor必须从predecessor已安装的`PLUGIN_AGENT_CONTEXT`继承同一activation snapshot，而不是重新读取current Plugin package。它与Round 5B继承active Skill body的理由相同：这是same task已选择的启动说明，不是current catalog。Task terminal后释放。

### 7.5 Hooks中的agent_type

`SubagentStartEvent`/`SubagentStopEvent`：

- 使用preset时`agent_type=<plugin-id>:<namespace-segment>...:<agent-name>`；
- 否则使用Round 10 built-in profile name；
- `agent_id`为exact canonical task id；
- worker transcript不复制给Hook；只有existing bounded last assistant/result view可用。

---

## 8. Hook integration inherited from Round 9.2

### 8.1 No new event or context contract

Round 9.3不增加Hook event、lifecycle seam、control vocabulary或provider source。Plugin-derived definition与USER/WORKSPACE definition使用Round 9.2同一套：

```text
HookEventType (11 events)
KernelHookDispatcher
HookCommandExecutor
ContextSourceKind.HOOK_CONTEXT
FrozenHookContextBatch
```

Event input/output、matcher aliases、sync/background、timeout、process-group cleanup、PULSARA_API_KEY scrub、ToolResult public projection、Stop/SubagentStop continuation与Pre/PostCompact顺序全部直接继承Round 9.2。若两篇文档冲突，以Round 9.2 generic runtime contract为authority；Round 9.3只能增加Plugin source normalization。

### 8.2 Plugin-specific event provenance

Generic Hook stdin中的source provenance对Plugin definition增加：

```text
source_kind = plugin
plugin_id
package_install_id
plugin_scope = USER | WORKSPACE
plugin_config_relative_path
```

这些字段只用于inspection、diagnostic与handler自识别，不授予permission或package mutation。Provider-visible`HOOK_CONTEXT`可以显示bounded Plugin source label/event/status，但仍是source-neutral`UNTRUSTED_OBSERVATION`；不得恢复`PLUGIN_HOOK_CONTEXT`。

### 8.3 Refresh ordering

Plugin composition与Round 5B/10 lifecycle的顺序：

- `reload_plugins`的Pre/Permission/Post使用predecessor generic Hook view；
- PreCompact使用predecessor view；
- 若PreCompact未abort，Plugin refresh把new contribution set交给generic dispatcher；
- PostCompact与ROOT SessionStart(compact)可观察new view；
- active child compaction继承其既有preset snapshot，但Hook definitions按generic exact-scope current view执行；
- SubagentStart/Stop仍由Round 10 existing seams触发，不由Plugin adapter直接调用dispatcher。

Plugin adapter不能绕过generic event producer，不能重跑一个event，也不能把Hook output写入Plugin state。

### 8.4 Dogfood expectation

Plugin Hook dogfood只需证明adapter复用已激活runtime：

1. package default/manifest Hook path成功normalize；
2. generic `hooks inspect/trust`使exact Plugin source runnable；
3. User/Workspace custom Hook与Plugin Hook按defined order共同运行；
4. real Tool/compaction/subagent event仍只dispatch一次；
5. output进入同一个`HOOK_CONTEXT`；
6. package replace使generic trust变MODIFIED；
7. disable/remove/reload后future Plugin handlers消失，old attempts正常drain；
8. architecture证明没有第二套matcher/executor/parser/context owner。
## 9. Owners 与依赖方向

### 9.1 Owners

| Owner | 拥有 | 明确不拥有 |
|---|---|---|
| `PluginPackageStore` | immutable version roots、state/data文件的atomic local lifecycle与opportunistic GC | Hook trust、Host runtime、MCP slot、Skill discovery |
| `PluginRuntimeCompositionOwner` | current enabled exact package values、consumer-lifetime package borrows、safe-point refresh | cross-owner atomic transaction、provider prefix、version history |
| `PluginSkillMaterializer` | exact Skill目录copy/provenance | Skill catalog/activation |
| `PluginHookContributionAdapter` | Plugin Hook config定位、host-profile normalization、package provenance/environment overlay与generic source identity | trust persistence、event dispatch、Hook process、provider context |
| existing `LocalSkillProvider` | four-root scan与Skill source | Plugin lifecycle |
| existing MCP supervisor/tool runtime | config/slot/catalog/route/attempt | Plugin enablement |
| `KernelHookDispatcher` | normalized current definition set、command attempts、exact-scope background pending buffer与queue-item-bound prompt context | Plugin parsing/trust、canonical rows、permission policy、tool execution |
| `PluginAgentPresetDirectory` | exact preset values与opaque refs | task scheduling、child execution |
| existing Round 10 coordinator | task/start material/worker scheduling | Plugin package parsing |
| existing compiler/continuity | provider context selection与prefix CAS | Hook process、Plugin install |

### 9.2 Forbidden dependency direction

```text
capability/*             must not import plugin runtime
hooks/*                  must not import plugins/*
MCP supervisor           must not import plugin parser
LocalSkillProvider       must not scan plugin package roots
Round 10 coordinator     must not parse plugin manifests/files
Plugin package core      must not import provider adapters/repository/compaction
Hook executor            must not write canonical conversation rows
```

Plugin adapter只输出现有owner需要的pure config/value。Host composition layer是唯一接线点；依赖方向固定为`plugins/hook_adapter.py -> hooks/contracts.py + hooks/parser.py`，绝不能反向。

---

## 10. Implementation slices

### R9.3-0：Package core 与 local lifecycle

- manifest/profile parser；
- containment与streaming atomic install；
- user/workspace state、shadow规则与doctor；
- component-level dispositions；
- 无package/contribution fingerprints。

### R9.3-A：Skill/MCP composition

- four-root Skill materializer/provenance；
- single MCP config composition；
- explicit Host reload；
- mid-session catalog successor与cold adoption retained tests。

### R9.3-B：Plugin Hook contribution adapter

- 复用已ACTIVATED的Round 9.2 contracts/parser/trust/dispatcher/executor/context；
- `plugins/hook_adapter.py`只拥有package定位、provenance/environment overlay与generic source construction；
- 为generic source union增加current Plugin branch；
- `reload_plugins`发布new contribution values，`reload_hooks`只重验current Plugin source trust；
- architecture gate禁止第二套Hook machinery。

### R9.3-C：Hook integration retained tests

- Round 9.2 human/tool/permission/result/compaction/subagent/ROOT seams不发生语义漂移；
- Plugin source与USER/WORKSPACE source共用一次dispatch；
- package reload/replace/disable与old-attempt drain。

### R9.3-D：Subagent presets

- Claude safe subset parser；
- `list_agent_presets` + opaque ref；
- Round 10 schemas/start material；
- `PLUGIN_AGENT_CONTEXT` child cold source。

### R9.3-E：Activation

- full tests/architecture/oracle；
- real MCP/Skill/Hook/Subagent dogfood；
- update active specs、Gap Index与README；
- activation evidence只记录行为与环境，不记录逐文件/document/evidence SHA。

---

## 11. Production modification map

### 11.1 New package

```text
src/pulsara_agent/plugins/
  contracts.py
  manifests.py
  package_store.py
  composition.py
  skills.py
  mcp.py
  hook_adapter.py
  agents.py
```

### 11.2 Existing integration

- `hooks/contracts.py`、`hooks/parser.py`、`hooks/trust.py`、`hooks/dispatcher.py`：增加Plugin source adapter输入与generic trust/reload join；不改变event/runtime semantics；
- `conversation_kernel/host.py`：package composition、reload与Plugin contribution publish；
- `conversation_kernel/subagent.py`、`subagents/contracts.py`：preset ref、start/stop seams；
- `conversation_kernel/cold_epoch.py`：optional `PLUGIN_AGENT_CONTEXT` exact seed；
- `conversation_kernel/context_sources.py`、`model_input/contracts.py`：一个新`PLUGIN_AGENT_CONTEXT` untrusted source kind；
- `capability/builtin_catalog.py`、`conversation_kernel/tool_runtime.py`：`list_agent_presets`、Round 10 optional ref字段，以及固定ROOT-only `reload_plugins` descriptor/authorize/invoke seam；
- `mcp_config.py`、`conversation_kernel/mcp/sdk_facade.py`与existing supervisor adapter：Plugin normalized config input、validated exact cwd与literal public headers；
- CLI：local package lifecycle；Hook trust继续使用Round 9.2 generic commands；
- active Round 9/9.1/5B/10 docs：只写消费接缝，不复制本文engine。

不得新增PostgreSQL migration或Protocol event kind。

---

## 12. Test plan

### 12.1 Package conformance

- Published Agent Plugins 1.0 fixtures；
- required schema/name与unknown-field rules；
- safe in-root symlink dereference，以及escape/dangling/cycle/identity-race拒绝；
- POSIX executable bit归一化后portable `./bin/server`可启动；setuid/setgid/sticky/ACL/xattr/ownership不被复制；
- fixed `skills/`/`mcp.json`；
- Codex/Claude adapters与multi-manifest priority；
- conformance fixture覆盖Codex当前公开的11项events（含`SessionEnd`）；固定本地源码checkout缺失该event只能成为research diagnostic，不能缩窄parser vocabulary；
- path escape、symlink/special file拒绝；
- streaming copy对exact active `PULSARA_API_KEY`值做跨chunk匹配；命中时temp root删除、diagnostic不含value，其他内容不扫描；
- component failure isolation；
- no total Plugin/package history cap。
- repr/log/doctor只排除exact `PULSARA_API_KEY`，其余真实package/Hook/MCP内容保持可诊断；

### 12.2 Lifecycle与scope

- user/workspace visibility；
- per-instance state分片与instance-local lock；generic Hook trust仍由Round 9.2独立per-source state拥有；一个Plugin replace不重写或锁住无关Plugin state；
- state directory的ordered-enumeration cut；enumeration后新增进入下一refresh，已枚举分片消失/变化使整个new package tuple unavailable且不发布partial composition；
- user/workspace同Plugin实例的Skill/MCP/Hook/preset leaf precedence与doctor projection；
- add/replace/enable/disable/remove atomic state与immutable version roots；
- add默认disabled；enable/disable保留current install；replace保留enablement但因new install id强制generic Hook trust变MODIFIED；remove删除state且orphan trust不能在缺少current source时授权；
- state atomic replace/delete是唯一lifecycle linearization；post-state Skill reconcile失败留下typed drift，future或running Host下一次complete Skill scan前同步重建且不回滚state；
- two-Host concurrent pre-scan reconciliation复用exact instance lock并在锁内重读state/install/provenance；旧观察不能覆盖new state，也不存在global reconcile mutex；
- running consumer borrow阻止旧root GC；successful reload后old Hook/MCP attempts/slots drain即释放，Host close/crash只是兜底，不得随reload次数无界积累version handles；
- replace后未reload Host继续使用old exact root/trust，reload或new Host只使用new root；
- running Host explicit reload；
- lifecycle/trust CLI明确报告running Host需要reload；disable/remove/revoke后old borrowed Hook/MCP在reload完成且old attempts/slots drain前仍可能有效，不伪造instant cross-process revocation；
- ordinary cold/compaction在capability cut前自动refresh current Plugin state；same-epoch非cold变化仍要求explicit reload；
- fixed `reload_plugins` descriptor在all modes保持相同，non-BYPASS typed denied；BYPASS调用只采用已提交state并返回per-component dispositions；
- reload工具的Pre/Post Hook使用predecessor frozen view，新Hook只从下一event生效；
- interrupted enable/disable留下的materialized Skill drift在future或already-running Host的下一次complete Skill scan前同步reconcile；无法完成时整个聚合Skill source为typed UNAVAILABLE，stale Plugin Skill不得重新进入catalog；conflict只形成doctor diagnostic且不覆盖用户内容；
- state已删除但projection cleanup未完成的orphan Skill必须通过provenance-sidecar enumeration被发现；digest匹配时删除，用户已修改时只摘除Plugin provenance并保留ordinary Skill；
- future Host restart读取state；
- Hook enable不等于trust。
- trust inspection明确告知Hook command本身可产生Host-level side effect，Tool permission只约束被Hook影响的后续Tool call；

### 12.3 Skill

- 只物化到existing `.pulsara/skills` roots；
- `.agents/skills`仍可由ordinary user install使用，但Plugin不写入；
- 不存在Plugin root/fifth root；
- name/body/supporting files byte-preserving；
- collision、modified ownership与disable cleanup；
- source携带reserved provenance sidecar拒绝；content digest复用existing `compute_skill_dir_hash()`并排除sidecar，禁止第二种tree hash；
- catalog COMPLETE/UNAVAILABLE/safe-point successor；
- compaction retained Skill继续使用Round 5B现有机制。

### 12.4 MCP

- portable/Codex/Claude config normalization；
- profile envelope exact：portable `$schema+mcpServers`、Codex direct/`mcp_servers`、Claude direct/`mcpServers`；cross-profile key guessing、Claude `.mcpb`与Codex `.app.json`不得进入MCP execution；
- namespaced server identity与collision；
- existing configured-server bound越界时MCP reload整体不发布且old exact supervisor config继续，其他Plugin component不被回滚；
- Plugin root/data expansion；
- Plugin MCP固定optional、ROOT_AND_SUBAGENTS且无permission/effect/required authority；单server connect failure不阻塞Host或其他component；
- enable inspection完整显示normalized MCP command/cwd/env keys或HTTP endpoint，并明确enable授权server spawn/connect而remote tool invocation仍需existing permission/effect gate；
- portable stdio command不做placeholder/shell split，args/env/cwd只展开标准变量；exact plugin/data cwd可启动；
- placeholder expansion单次且non-recursive；portable env保留名、unknown placeholder与all-loopback-IP URL goldens；
- stdio child删除`PULSARA_API_KEY`，Plugin env/secret/header ref不能读取或重新加入；
- Streamable HTTP literal public headers、no redirect/cross-origin forwarding；legacy SSE typed unsupported；
- late READY -> META；
- late META真实调用的Pre/Permission/Post Hook只对resolved remote identity触发一次，不对`use_new_mcp_tool` wrapper重复触发；
- compaction/cold -> DIRECT when cohort fits；
- disconnect/uninstall gate；
- same identity schema replacement不meta绕过；
- Plugin runtime不拥有slot/attempt/policy。

### 12.5 Plugin Hook adapter

- Codex/Claude path、array、inline与default `hooks/hooks.json` forms；
- adapter调用Round 9.2唯一parser，unsupported event/handler使用upstream typed disposition；
- Plugin source identity覆盖instance scope、install id与config identity；
- USER/WORKSPACE Plugin component winner在generic source publish前机械确定；
- `PLUGIN_ROOT/PLUGIN_DATA`及Claude aliases使用exact managed roots；
- placeholder expansion single/non-recursive，unknown/cycle/escape拒绝exact handler；
- Plugin source不能覆盖Host-owned environment或读取`PULSARA_API_KEY`；
- enable不trust；generic `hooks inspect/trust` happy path；
- replace使generic trust MODIFIED；remove后的orphan trust不能授权；
- `reload_hooks`只重验current installed Plugin source trust，`reload_plugins`才重读package config；
- old Plugin Hook attempts settle后及时释放package borrow；
- architecture gate证明`hooks/*`不导入`plugins/*`，且Plugin没有parser/matcher/executor/output/context副本。

### 12.6 Generic Hook retained integration

- Round 9.2全部11-event targeted tests原样通过；
- USER/WORKSPACE custom Hook与Plugin Hook按defined order共用一次dispatcher；
- late META真实调用仍只对resolved remote identity触发一次；
- package reload自己的Pre/Permission/Post使用predecessor view；
- PreCompact predecessor -> Plugin refresh -> PostCompact/new SessionStart顺序；
- output只进入source-neutral `HOOK_CONTEXT`；
- no new Hook process cap、trust store、lifecycle seam或durable machinery。

### 12.7 Subagent preset

- recursive default `agents/**/*.md`、manifest single-file/directory/array declarations与relative namespace identity；physical duplicate去重，nested同名basename不扁平冲突，exact scoped-name冲突typed unavailable；
- simple Claude agent parse/list/ref/spawn；
- directory page逐行完整、40K FULL-required、cursor scope/current-view/offset exact；单行overbound typed unavailable；
- unsupported tool/model/permission/maxTurns fields使preset unavailable；
- stale/tampered/wrong-scope ref拒绝；
- existing Round 10 task graph/result/dependency行为不变；
- child tools/permission/model不因preset变化；
- body进入`PLUGIN_AGENT_CONTEXT`而非SYSTEM；
- Plugin update before queued child start不改变accepted task已经冻结的preset正文；new task必须取得current ref。

### 12.8 Prefix与architecture

Chat Completions与Responses都证明：

- same epoch SYSTEM/tools exact；
- messages只追加suffix；
- Plugin reload不热改native surface；
- compaction/child first-open只通过shared cold assembler；
- 七维oracle保持`29 / 24 / 11 / 1 / 25 / 0 / 11`，最后一维为既有Hook event types；
- 无durable Hook/Plugin execution machinery；
- 无新增skip/xfail；
- 无per-file/document/evidence SHA gate。

### 12.9 Real dogfood

至少使用一个真实OpenAI-compatible provider执行：

1. Host cold start时Plugin Skill被catalog发现并由普通`read_file`完整读取；
2. 同一ROOT epoch中enable一个MCP Plugin，模型从catalog使用`inspect_new_mcp_tool -> use_new_mcp_tool`；
   enable后由模型显式调用固定`reload_plugins`，不重启Host且不改变native tools[]；
3. trusted `UserPromptSubmit`或`PreToolUse` Hook产生真实block与真实context；
4. background Hook output在下一safe point追加；
5. compaction触发Pre/PostCompact及`SessionStart(compact)`，successor tools按Round 9重新冻结；
6. list agent preset、spawn Round 10 worker并看到preset context；
7. CLI disable Plugin并显式`reload_plugins`后，future Hook/preset/meta route消失、next Skill scan先reconcile removal，旧native descriptor由unavailable gate拒绝；
8. 记录真实prompt、Hook stdin/stdout、模型回复与MCP/Skill路径；只排除`PULSARA_API_KEY`。

---

## 13. Definition of Done

1. Portable core严格符合Agent Plugins 1.0.0 Published contract；
2. Codex Skill/MCP/Hook package happy path可运行；
3. Claude simple agent preset happy path可运行；
4. Plugin不是capability leaf或executor；
5. Skill没有第五root；
6. MCP没有第二registry/supervisor；
7. Hook只有Round 9.2一套与`plugins/`平级的generic trust/dispatcher/executor/provider context；Plugin只拥有contribution adapter，不拥有trust state；
8. agent preset复用Round 10 task与shared cold assembler；
9. unsupported vendor semantics被typed拒绝或skip，不被静默放大authority；
10. trust、permission、effect与canonical authority边界闭合；
11. same-epoch strict prefix成立；
12. ordinary cold open、compaction successor与child first-open只复用既有shared cold-epoch assembly；不新增第三种rebase boundary；
13. 没有任意total Plugin/Hook/task lifetime caps；
14. 没有冗余DTO fingerprints、SHA evidence循环或compatibility fallback；
15. 无schema/event/job/guard/recovery增长；
16. full pytest、PostgreSQL、architecture、compiler/continuity、Round 9/9.1/5B/10 retained tests与real dogfood全部通过；
17. 规格、Gap Index、README与activation evidence只描述新单一路径。

---

## 14. 最终冻结

Round 9.3激活后的产品语义是：

> Pulsara可以从本地安装Agent Plugins 1.0、Codex或Claude Code风格的Plugin package。Portable Skills被原样物化进Round 9.1现有四根目录之一；MCP definitions进入Round 9唯一supervisor并沿用direct/meta和cold adoption；已信任的command Hooks在Codex当前公开的11项native lifecycle seam运行，其中`SessionEnd`同时兼容Claude，其控制结果只能影响当前合法操作，其模型可见文本始终作为append-only untrusted observation；简单Claude agent preset通过opaque ref为Round 10新worker提供低authority启动说明。Plugin本身不成为capability、executor、permission owner、canonical authority或durable recovery system。中途refresh不改写已安装provider prefix，只有existing cold open和compaction successor重建SYSTEM/tools。
