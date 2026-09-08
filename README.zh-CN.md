# 🪐 Pulsara

<p align="center">
  <img src="assets/banner.png" alt="Pulsara" width="100%">
</p>

Pulsara 是一个围绕小型关系型 conversation kernel 构建的本地优先 Agent
Runtime。PostgreSQL 保存已经接受的产品事实；provider stream、terminal
process、subagent 与 UI draft 均只存在于当前进程。

项目仍处于快速开发阶段。接口可能变化；在产品早期，数据库采用 reset-only
migration universe。

[English](README.md) · [简体中文](README.zh-CN.md) ·
[长期契约](contracts/README.zh.md)

## 架构

```text
Python KernelHostCore
├── foreground conversation coordinator
│   ├── exact turn admission + provider dispatch
│   ├── compaction + Plan + Tool batch coordinators
│   └── memory + steer dispatch support
├── provider-neutral structured model-input compiler
├── tool policy + Host-scoped physical tools
├── foreground safe-point compaction + snapshot adoption
├── advisory memory governor + retrieval
├── process-local live event bus
└── renderer-neutral Protocol v3 gateway

PostgreSQL
├── pulsara_v3：28 张产品关系
├── selective agent_events occurrence journal
├── public.vector capability
└── public.pulsara_schema_migrations（只保存 universe metadata）
```

Durable 边界有意保持狭窄：

- canonical relational rows 拥有 conversation、tool 与 coordination 的
  当前语义真值；accepted memory row只拥有advisory dataset当前包含的内容；
- closed 29-type `agent_events` journal 只记录 accepted occurrence，不用于恢复
  execution；
- 24 种 live event 只存在于内存，进程退出即可丢失；
- 当前不存在durable job handler或job relation；
- tool request 在 dispatch 前提交，physical attempt 在 effect invoke 前提交；
- crash 会中断 active turn；reopen 只 rehydrate 已接受的 conversation facts，
  不恢复 coroutine 或 provider stream；
- derived UI、audit、search 与 notification state 不得否决 canonical commit 或
  Host close。

Pulsara 不再使用 universal EventLog、execution replay、durable model segment、
projection-job framework、Oxigraph、SPARQL 或 generic runtime-write admission
epoch。

## 当前产品面

Kernel 当前支持：

- 从已结算最终回复发起[会话分叉](PULSARA_CONVERSATION_FORK_EFFECTIVE_CONTEXT_COPY_SPEC.zh.md)：
  把分叉点当时的有效摘要与保留历史复制到独立、空闲的新会话，不重跑原来的执行。
  保留该点的历史模型选择；文件与当前 advisory memory 仍共享同一 workspace/domain；
- OpenAI-compatible Responses 与 Chat Completions transport；
- 显式provider-neutral `COMPLETED | OUTPUT_INCOMPLETE | PROVIDER_ERROR`
  model terminal、whole-response atomic assistant acceptance，以及不依赖
  provider-held response state或remote response ID、可跨Host/进程重启恢复的
  accepted Chat/Responses exact native replay；
- 基于 exact canonical cut 的 provider-neutral structured input compiler：
  closed typed first-party source、按 scope 冻结的 tool schema、目标 estimator
  精确计量、source/tool-result 的确定性降级，以及一次冻结的
  semantic-plus-actual-wire continuity proof；
- bounded、脱敏的上一turn outcome guidance与append-only tool freshness
  frontier；每条accepted tool result携带immutable observed time、monotonic
  duration disposition、execution origin及optional trusted duration，tool body
  无法伪造这些outer timing facts；
- revision 锚定的 filesystem tools：`read_file` 返回原始字节的精确 SHA-256
  revision 并记录 process-local seen lines，`edit_file` 只对该已观察 revision
  执行确定性行操作，`write_file` 以 atomic no-clobber 方式仅创建新文件；以及
  `terminal`、`terminal_process`、`terminal_monitor` 与 scoped `artifact_read`
  tools；
- exact-run、process-local 的 `todo(items=[...])` 工具：一次原子替换一个
  bounded pending/in_progress/completed snapshot，空list显式清除，ROOT与child
  相互隔离，Host replacement明确不恢复TODO state；
- 真实 PIPE/PTY Terminal output streaming、exact process-local cursor、typed
  GAP、same-Host future monitor observation，以及 provider safe-point 上的
  autonomous continuation；
- default-deny subprocess environment、bounded login-shell snapshot、最近
  `.venv/bin`、foreground cwd continuity，以及 Host close 时的 physical
  process-group drain；
- 通过 shared blob store 保留完整 sanitized tool output：中等输出完整
  provider-neutral logical ToolResult不超过40,000 UTF-8 bytes时可保持FULL
  （与adapter physical wire bytes相互独立）；更大输出使用UTF-8-safe的8,000字符
  head/tail preview，并按需有界读取；
- ROOT编排的Host-scoped worker task graph：batch DAG admission、dependency
  scheduling、partial multi-wait、exact task stop、boundary-safe ROOT-to-worker
  message、explicit/inferred canonical result与bounded `NONE | LAST_N` parent
  context；worker始终是不可递归的leaf；
- 符合Agent Skills标准的bundled/local skills，只扫描workspace/user各自的
  `.pulsara/skills`与`.agents/skills`四种exact root，通过单一聚合、append-only
  `SKILL_CATALOG` source投影，不获得execution或permission authority；catalog
  routing metadata只能完整发布或明确UNAVAILABLE，explicit/configured activation
  携带exact parsed Markdown，模型驱动的progressive disclosure只使用普通
  `read_file`及其2,000行窗口；
- 统一的process-local capability discovery：execution-backed Builtin、每server
  MCP snapshot与聚合Skill catalog进入同一个pure frozen registry，physical
  authority仍由原owner持有；
- Host-scoped stdio/Streamable HTTP MCP：bounded discovery、cold direct tools、
  late/native-incompatible meta inspect/use、typed unavailable gate、
  catalog/resource/prompt读取、local authorization与CLI lifecycle管理；
- advisory PostgreSQL memory：每次`remember`只提交一个candidate，四类closed
  taxonomy，global与exact workspace applicability隔离，best-effort governance，
  多语种sparse recall，optional 1024维dense recall与explicit rerank，以及
  direct/reverse/最多two-hop relation read；
- foreground safe-point context compaction提供manual、proactive与mid-turn三条
  入口；summary adoption保留canonical transcript与pairing-safe protected tail，
  并在标准cold capability epoch中继续，不使用durable compaction job；
- canonical Inspector 与 Protocol v3 terminal observation。

## Frontend

仓库现在包含 [`frontend/`](frontend/) 下的 Pulsara 本地智能工作台。它直接连接本机
Host，只展示已有 Kernel 契约支持的总览、持久会话、执行轨迹、规划交互、逐轮权限、
输入队列、停止、上下文整理、子任务和本地配置。子任务会原位出现在委派它们的主消息下，
逐项展示目标、实时工具活动、真实状态和 Markdown 结果；任务页提供同一事实的全局视图。
前端不再使用 demo projection，也不会为尚未
接入的能力保留占位入口。新建会话只选择“快速开始”或“指定目录”：前者由 Pulsara
创建可恢复的持久工作目录，后者使用现有绝对路径。规划和权限都在输入框旁按单轮选择。
浏览器可直接打开裸 loopback 地址，不需要 token、cookie、OpenAI 或其他网站账号登录。产品契约见
[`PULSARA_FRONTEND_APPLICATION_SPEC.zh.md`](PULSARA_FRONTEND_APPLICATION_SPEC.zh.md)。

Round 6不增加durable MCP连接或request recovery；Host换代只按配置fresh
connect。form/private URL elicitation、OAuth、MCP-backed skill activation、
server-initiated Sampling/Roots、Apps/Tasks与bundled terminal UI仍是明确
non-goal。未来Web或desktop client可以消费Protocol v3，但不得成为第二套
canonical authority。
普通Host启动时，workspace自带的MCP entry默认保持disabled；只有用户显式传入
`--trust-workspace-mcp`才会激活。仅仅打开一个仓库不会执行其中的stdio command，
也不会解析其HTTP secret reference。

## 环境要求

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL，且 `public.vector >= 0.5.0`

## 初始化

```sh
uv sync
```

Pulsara 不再从 `.env` 或产品专用环境变量读取配置。启动本地应用后，在“设置”中：

- 保存本机 PostgreSQL runtime DSN 与可选 admin DSN；
- 显式检查 runtime database，或使用 admin DSN 初始化/迁移；
- 选择 provider 与 models.dev 目录中的模型，选择 Chat Completions 或 Responses，
  并添加 API key；
- 可选地分别填写 DashScope embedding 与 rerank 两枚独立 key。

PostgreSQL DSN、模型配置与全部 API key 一同保存在 closed
`${PULSARA_HOME}/local-settings.yaml`（或 Pulsara 默认 home）中；目录权限为 `0700`，
文件权限为 `0600`。API key 在 UI 中仍为 write-only，本地 API 不会返回其值。零配置也能启动
应用和设置壳；缺少任一 DashScope key 时 sparse memory recall 仍可使用。

```sh
uv run pulsara app
uv run pulsara config-check
uv run pulsara db verify --deep
```

第一条命令会启动仅限本机访问的 Web 应用并打开浏览器；使用 `--no-open` 可以只启动
服务。页面关闭或服务重启后，持久会话与其原工作目录仍可继续。

唯一 active migration universe 是
`pulsara.conversation-kernel.v1` generation 1，从 version 0 开始。Round 1 与
Round 2 在保持 exact 24 张产品关系的同时更新了 version-0 `tool_results` 与
canonical Terminal-observation 契约；当前 baseline/catalog identity 与验证证据记录在
[`round1_tool_output_artifact_activation.json`](benchmarks/suites/core/v1/round1_tool_output_artifact_activation.json)
和
[`round2_terminal_runtime_activation.json`](benchmarks/suites/core/v1/round2_terminal_runtime_activation.json)。
Round 3 不改变数据库 universe；其 process-local compiler 与双 provider
验证记录在
[`round3_structured_model_input_compiler_activation.json`](benchmarks/suites/core/v1/round3_structured_model_input_compiler_activation.json)。
Round 3.1 增加 Host-scoped、process-local provider-input continuity epoch：在
同一 exact ROOT 或 child scope 内，system prompt 与 tool surface 保持不变，
canonical conversation fact 和 typed runtime observation 只作为 message suffix
追加。busy `Enter` steer exact active ROOT turn，`Tab`排队 future new turn。
Host replacement 只从 canonical rows 冷启动。Round 5A.2只持久化已接受
assistant entry的bounded private Chat/Responses native replay carrier；不持久化
compiled provider conversation、remote response identity或in-flight stream。Steer prefix planning复用已安装prefix
estimate，共享一个cooperative absolute deadline；unique-work quote不会为每个
nested-prefix trial重复收取同一immutable base。provider open还必须消费Host
continuity owner密封签发的exact one-shot permit对象，compiler则强制每种
first-party source恰好一个VALUE或ABSENT branch；
Plan handoff显示正文与exact canonical transition identity相互分离。证据记录在
[`round3_1_provider_input_prefix_continuity_activation.json`](benchmarks/suites/core/v1/round3_1_provider_input_prefix_continuity_activation.json)。
Round 5A.2跨进程replay验证记录在
[`round5a2_durable_provider_replay_and_cross_restart_thread_continuation_activation.json`](benchmarks/suites/core/v1/round5a2_durable_provider_replay_and_cross_restart_thread_continuation_activation.json)。
Round 4将clean-v0扩展为26张产品关系和34类selective occurrence；Plan
workflow、run-bound permission、Protocol v3与real-provider证据记录在
[`round4_plan_workflow_and_run_permission_activation.json`](benchmarks/suites/core/v1/round4_plan_workflow_and_run_permission_activation.json)。
Round 5A删除ROOT与child turn的固定model/tool-call次数和turn-wide wall-clock
deadline。每次provider-dispatch planning、canonical operation、provider
transport、physical tool、writer renewal、Terminal decision和close分别使用自己的
closed watchdog；foreground provider stream只有connect/write/pool/read-idle边界，
没有total response timeout。该checkpoint中的finite durable job仍保留bounded
attempt total。证据记录在
[`round5_long_horizon_execution_envelope_activation.json`](benchmarks/suites/core/v1/round5_long_horizon_execution_envelope_activation.json)。
Round 5B现已增加manual、proactive与mid-turn safe-point compaction。当前主模型在
禁用工具的old exact prefix上生成summary；active adoption随后从snapshot、最近真实
用户输入、pairing-safe protected tail、current Runtime observation与bounded retained
Skill context建立标准cold epoch，并在同一run继续。canonical history永不改写，
provider-error reactive retry仍不支持，最后一套durable job machinery已经删除。
Round 5B激活时oracle为28类Committed event、24类Live event、11个subject slot、
1个append guard、24张product relation和0类durable job。验证记录在
[`round5b_long_horizon_context_compaction_activation.json`](benchmarks/suites/core/v1/round5b_long_horizon_context_compaction_activation.json)。
Round 7在existing `tool_results` relation中增加immutable observation
timing/origin facts，并为immediate predecessor outcome与per-turn freshness
frontier增加两个provider-neutral compiler source。同一compatible Host/scope
epoch内不会重写旧provider message；late result与freshness变化只形成新suffix。
Pulsara-owned provider carrier只保留产品语义与lifecycle，internal contract
version、fingerprint、generation、schema marker和delimiter式Plan carrier不再进入
model input。验证记录在
[`round7_model_visible_failure_and_tool_observation_activation.json`](benchmarks/suites/core/v1/round7_model_visible_failure_and_tool_observation_activation.json)。
Round 7.1让全部tool origin共用唯一provider-visible projection ladder。40,000-byte
上限只约束provider-neutral logical ToolResult；Chat/Responses物理request bytes仍由
exact wire plan拥有。FULL不合法时variant可从COMPACT/REF_ONLY/OMITTED开始；成功的
`artifact_read` page则必须exact FULL交付，否则在provider open前以typed resource
boundary停止。Artifact guidance保持conditional，canonical result不因budget被改写，
same-epoch已安装message仍只允许append suffix。验证证据记录在
[`round7_1_provider_visible_tool_result_projection_activation.json`](benchmarks/suites/core/v1/round7_1_provider_visible_tool_result_projection_activation.json)。
Round 9用一个pure、provider-neutral capability registry替换旧Tool/Skill平行
exposure结构；registry只能由Builtin、MCP与聚合Skill三个原owner签发的snapshot组装。
Exact target-aware native preflight先于parent dispatch cut，Tool planner与Skill
projection只消费该cut派生的sibling views。Cold MCP cohort必须同时满足canonical与
actual-wire双界才整体direct，否则整体meta；late-ready和native-wire-incompatible
tool通过bounded `inspect_new_mcp_tool`再`use_new_mcp_tool`执行，policy/route-bound
ref只有在inspect result以FULL安装后才可调用。同epoch的SYSTEM/tools保持byte-stable，
catalog/route变化只追加message。本轮没有新增capability relation、event、job、
receipt、generation或recovery graph。验证证据记录在
[`round9_unified_capability_semantics_activation.json`](benchmarks/suites/core/v1/round9_unified_capability_semantics_activation.json)。
Round 9.1以portable Agent Skills core替换legacy Pulsara Skill frontmatter契约。
Skill owner通过一个scope-bound policy扫描exact four roots、全局解析precedence，并只向
既有Round 9 registry贡献一个聚合`LOCAL_SKILL_CATALOG` snapshot。单个invalid manifest
只产生bounded diagnostic；complete scan无法证明或overbound时发布一个UNAVAILABLE
successor，不发布partial catalog。Catalog与active body变化只追加messages，SYSTEM和
tools保持稳定。`allowed-tools`等host-specific字段保持inert，Skill正文不能授予tool、
permission、MCP route或execution authority。普通`read_file`是唯一progressive
disclosure路径：它没有Skill intent或loaded-state，重复读取返回current bounded bytes。
验证证据记录在
[`round9_1_agent_skills_standard_activation.json`](benchmarks/suites/core/v1/round9_1_agent_skills_standard_activation.json)。
Round 10把flat child升级为由唯一ROOT拥有的worker task graph。七项ROOT-only
orchestration tool在全部ROOT permission mode中保持provider-visible，但只有
`BYPASS_PERMISSIONS`可执行；worker只获得`report_agent_result`，不能创建后代。
Stable task/dependency row拥有logical board，scheduler、capacity、mailbox与wait仍为
process-local。四child只是physical concurrency，不是task graph生命周期上限；更多
accepted work保持`PENDING_START`，capacity释放后继续启动。Direct dependency result只
传播一条edge，ROOT通过list/wait/accept显式观察result。Round 5B compaction handoff复用
同一task board，不增加durable inbox、run、receipt或recovery graph。当前oracle为29类
Committed event、24类Live event、11个subject slot、1个append guard、28张product
relation和0类durable job。验证记录在
[`round10_hierarchical_subagent_orchestration_activation.json`](benchmarks/suites/core/v1/round10_hierarchical_subagent_orchestration_activation.json)。
Fingerprint subtraction hard-cut一次性删除same-process self hash、重复的
child/parent proof字段、依赖fingerprint回查的continuity/settlement路径，以及逐文件
activation SHA清单。Process-local authority现在由exact frozen object、owner slot、
nonce/revision与数据库约束表达；content integrity、durable confirmation、provider
prefix/replay compatibility、stable canonical identity和带密钥opaque token的digest
保持不变。Provider wire、Protocol v3、canonical rows与architecture oracle均未改变。
验证记录在
[`fingerprint_subtraction_hard_cut_activation.json`](benchmarks/suites/core/v1/fingerprint_subtraction_hard_cut_activation.json)。
Production code cleanup hard-cut保留同一foreground状态机，同时把turn admission、
provider dispatch、compaction、Plan control、tool execution、memory与steer settlement
迁入窄owner。Runner只决定下一阶段，不再重新实现这些authority；production中没有遗留
旧facade、兼容import、新fingerprint或durable recovery机制。验证记录在
[`production_code_cleanup_and_runner_decomposition_activation.json`](benchmarks/suites/core/v1/production_code_cleanup_and_runner_decomposition_activation.json)。
Round 8用advisory dataset取代旧memory durability/recovery graph。`remember`会与
ToolResult同事务接受一个candidate；governance、cheap-hint prompting、
embedding与reranking均保持可丢失的process-local弱完成。Accepted item只能是
FACT、USER_PROFILE、RESPONSE_PREFERENCE或DECISION。Sparse recall
始终本地可用；automatic dense recall与explicit rerank是optional remote data egress。
Memory只通过bounded append-only `MEMORY_RECALL`与
`MEMORY_RESPONSE_PREFERENCE_HEAD`进入model input，不改写已安装prefix，也不获得
permission authority。Sparse index与query共用tokenizer-v2，保留否定、先后词、
code/path token与英文contraction语义。明确“不要保存当前内容”只在该ROOT run中
关闭`remember`，recall仍可见；明确“本轮不使用已有记忆”会清除两个memory source
并在不改变advertised tool surface的前提下拒绝四个memory tool。短输入跳过automatic
recall仍是独立策略，不会关闭显式memory tool。验证证据记录在
[`round8_advisory_memory_subsystem_activation.json`](benchmarks/suites/core/v1/round8_advisory_memory_subsystem_activation.json)。
Lightweight TODO refinement用一次bounded完整snapshot调用替换旧Host-global
action协议，并由exact ROOT/child process-local owner持有current state。只有
canonical ToolResult成功结算后才安装snapshot；client接收单个原子live projection，
LIVE GAP后直接从当前Host owner完整同步。TODO始终是advisory而非durable，Round 5B
只能消费其只读actionable handoff。验证证据记录在
[`lightweight_todo_tool_refinement_activation.json`](benchmarks/suites/core/v1/lightweight_todo_tool_refinement_activation.json)。
旧 v13
数据库只会得到 `schema_migration_universe_reset_required`，不会被在线导入、
翻译或升级。请严格遵守
[clean-baseline runbook](archived_docs/STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md)，没有针对 exact
endpoint/database 的 operator 授权时，不得重置真实环境。

## 运行

本地 Web 应用是配置与对话的主要入口：

```sh
uv run pulsara app --workspace /path/to/project
```

会话已经显式选择模型连接后，可用 headless REPL 恢复该 canonical session，
不再经过环境文件：

```sh
uv run pulsara host repl \
  --workspace /path/to/project \
  --continue
```

## Client 边界

bundled Go TUI已经删除。Protocol v3继续作为未来Web或desktop client的
renderer-neutral传输边界。Python仍拥有canonical state、command、policy、
secret与recovery；client只能渲染snapshot、消费live event，并提交gateway
允许的typed command。Protocol v2与Presentation Foundation已完全退役。

## 开发门控

```sh
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q src tests
uv run python tools/generate_terminal_protocol_contract.py --check
git diff --check
```

PostgreSQL integration tests 使用 `postgres` marker：

```sh
uv run pytest -q -m postgres
```

## 长期契约

当前有效契约统一列在 [contracts/README.zh.md](contracts/README.zh.md)。根目录
research/hard-cut 文档解释历史决策，但不构成 runtime registry 或 compatibility
specification。
