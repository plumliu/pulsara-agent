# 🪐 Pulsara — 更能记住你的 Agent Runtime

<p align="center">
  <img src="assets/banner.png" alt="Pulsara" width="100%">
</p>

<p align="center">
  <strong>FEEL FREE TO VIBE WITH PULSARA</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/PostgreSQL-runtime%20truth-336791?style=for-the-badge&logo=postgresql&logoColor=white" alt="PostgreSQL">
  <img src="https://img.shields.io/badge/Oxigraph-semantic%20graph-5b21b6?style=for-the-badge" alt="Oxigraph">
  <img src="https://img.shields.io/badge/Python-3.12+-111827?style=for-the-badge&logo=python" alt="Python 3.12+">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
</p>

**Pulsara** 是一款面向长程工作的 _本地优先 Agent Runtime_。
它专为希望助手深度融入实际项目、驾驭漫长对话、铭记核心要素的开发者而生。
当任务错综复杂，它依然能清晰复盘执行始末。

系统专为真实且持久的工作流打造：Agent 能够读取文件、执行终端指令、遵循特定技能逻辑、
无缝恢复历史会话、智能压缩上下文，并持久化长期记忆，让每一次交互都产生持续复利。

**Pulsara 仍处于积极开发阶段，还不是一个成熟项目，可能会出现不少 bug，也会有许多粗糙边缘。
目前主要交互形态仍是 terminal REPL，未来可能会探索图形用户界面。如果你愿意尝试，欢迎多提 issue、
多反馈真实使用体验，也希望我们能在互相学习中一起把它打磨得更好。**

[English](README.md) · [简体中文](README.zh-CN.md) · [核心理念](#核心理念重新定义长程工作流) · [架构概览](#架构概览分层可重放与可审计) · [记忆系统](#记忆系统聚焦未来决策的智慧资产) · [快速开始](#极速启航)

---

## 核心理念：重新定义长程工作流

许多 Agent 擅长在最初的十分钟惊艳四座，而 Pulsara 致力于为接下来的十小时保驾护航。

长程 Agent 交互已经演变为一个持续运转的生命周期：

- 模型会制定计划、发起确认，并在挂起后无缝恢复工作。
- 工具链持续生成日志、文件、中间产物、Artifact 以及错误状态。
- 上下文不断增长，系统需要在保留任务主线的前提下完成智能压缩。
- 模型可见上下文不能只是无限回放 transcript，而应由结构化运行时事实编译而来。
- 关键事实会沉淀为长期记忆，免于在冗长的对话记录中遗失。
- 未来操作支持深度追溯，精准回答“发生了什么”与“为何发生”。

Pulsara 将这一完整生命周期视为产品核心。

| 传统体验 | Pulsara 体验 |
| --- | --- |
| 会话随窗口结束而消散 | **持久化 Runtime**：全面记录 sessions、runs、turns、replies 与恢复机制。 |
| 对话记录难以复盘 | **可重放事件流**：类型化运行时事实与可深度洞察的 projection。 |
| 上下文越堆越重 | **Context compiler + compaction**：为后续模型交互生成有预算、有分区、有时间感知的上下文。 |
| 记忆缺少证据链 | **有根有据的长期记忆**：记忆与 evidence、artifact、relation、conflict 深度绑定。 |
| 工具调用难以解释 | **可审计工具执行**：权限决策、产物生成、技能归因与 observation timing 完整透明。 |
| 能力暴露依赖静态提示词 | **渐进式 capability surface**：统一承载工具、skills、MCP-style 接口与本地工作流。 |
| 委派工作变成隐藏聊天 | **Subagent runtime graph**：子 Agent 是带事件与结果的 runtime session，而不是不可见脚本。 |

Pulsara 超越常规模型 API 封装，逐步成长为完整的 Agent 操作系统：会话、工具、记忆、权限、
事件、存储、压缩与恢复在此交融成一个统一运行时。

## 架构概览：分层、可重放与可审计

Agent 的上下文理应具备分层、可重放与可审计的特性。Pulsara 以三层存储架构组织长程工作，
让运行时事实、执行产物与语义记忆各司其职。

```text
┌──────────────────────────────────────────────────────────────┐
│                         Agent Runtime                         │
│  model loop · context compiler · tool execution · subagents    │
│  plan mode · permission gate · capability exposure · resume    │
│  compaction · time-aware observations                          │
└──────────────────────────────┬───────────────────────────────┘
                               │ typed events
┌──────────────────────────────▼───────────────────────────────┐
│                    Layer 1 — Runtime Ledger                   │
│  sessions · runs · turns · replies · tool calls · plan state   │
│  context compaction events · capability decisions · inspector  │
└──────────────────────────────┬───────────────────────────────┘
                               │ artifacts + evidence
┌──────────────────────────────▼───────────────────────────────┐
│                  Layer 2 — Artifact & Evidence Store          │
│  large tool outputs · adaptive previews · archived refs        │
│  compaction summaries · execution evidence · replay anchors    │
└──────────────────────────────┬───────────────────────────────┘
                               │ governed memory writes
┌──────────────────────────────▼───────────────────────────────┐
│                   Layer 3 — Semantic Memory Surface           │
│  canonical memories · search index · conflicts · relations     │
│  PostgreSQL truth · Oxigraph semantic graph · recall traces    │
└──────────────────────────────────────────────────────────────┘
```

### 三层存储架构构建完整故事线

| 核心层级 | 存储内容 | 设计初衷 |
| --- | --- | --- |
| **运行时账本 Runtime Ledger** | 会话追踪、轮次记录、计划流转、工具调用、能力决策、压缩边界。 | 确保 Agent 随时可恢复且行为可解释。系统可精准定位任何中断、批准、压缩或失败事件的根源。 |
| **产物与证据库 Artifact & Evidence Store** | 大容量终端输出、网页抓取数据、生成文件、工具预览、压缩摘要与证据锚点。 | 兼顾模型所需的小型预览与人类用户的完整恢复需求，并为记忆提供坚实溯源依据。 |
| **语义记忆层 Semantic Memory Surface** | 规范化记忆、冲突标记、更替关系、搜索索引、图谱网络与召回轨迹。 | 赋予长期记忆多维度的语义、关联与溯源查询能力，跨越基础近邻文本匹配的局限。 |

Pulsara 让每一层专注其职，并通过稳定的事件 ID 与证据 ID 建立坚固联结。运行时负责事实，
artifact 负责恢复，memory 负责未来决策。

## 事件系统：重塑 Runtime 的骨架

Pulsara 的事件系统构成 Agent 的事实骨架。系统会为关键节点发出类型化事件：

- 模型文本生成与工具调用的实时流传输。
- 工具结果、产物、自适应预览与 observation timing 的生成。
- 权限决策与 capability gate 的拦截结果。
- 计划模式的进入、提问、修订、批准与取消全流程。
- context compile 尝试、section budget、render decision 与 diagnostic。
- subagent graph、任务状态、子 Agent 结果与结果投递。
- 上下文压缩的启动、完成或异常状态。
- 持久化会话与运行边界的精准界定。
- 记忆召回轨迹与治理结果。

这一事件流引擎同时赋能三大关键场景：

1. **会话恢复 Resume** — 随时重启会话，精准重建模型可见的对话上下文。
2. **深度洞察 Inspect** — 复现历史操作，免除盲目猜测与翻阅终端残影。
3. **上下文压缩 Compaction** — 基于真实运行时历史进行上下文压缩，确保摘要严谨连贯。

## Context Compiler：模型可见状态不是简单拼接

Pulsara 现在将 prompt 构造视为一个明确的运行时子系统。Context compiler 会收集类型化事实，
将它们划分为 section，应用 lifecycle cache，为每个 section 做预算，再降低成 provider 请求。

这意味着模型可见上下文拥有清晰结构：

- 稳定系统指令与运行时事实分离。
- memory projection、capability catalog、active-skill hint、runtime context、recovery note 与 subagent result 都是可检查的 section。
- 当前用户消息会锚定在 current-run tool tail 之前，保持 provider-native tool-call 顺序合法。
- 大型或历史 tool result 会通过 allocator 渲染，拥有 per-result decision、artifact-backed preview 与 bounded envelope。
- timing header 会告诉模型某个 section 是当前、历史、缓存、压缩摘要，还是后台进程观察。
- `ContextCompiledEvent` 会记录 sections、估算、诊断与 tool-result render decisions，供 inspector 复盘。

目标很朴素：让模型看到最小但有用的事实版本，同时让人类和恢复路径仍然能解释这份上下文是如何生成的。

## 记忆系统：聚焦未来决策的智慧资产

Pulsara 的记忆链路围绕 grounded evidence 展开：

```text
conversation → tool results → artifacts → evidence → memories → recall
```

系统聚焦留存会影响未来决策的关键信息：

- 用户核心偏好与持久化工作上下文。
- 项目推进过程中的关键决策链。
- 拥有明确证据支撑的事实断言。
- 矛盾事实及已迭代更替的旧版状态。
- 记忆、产物与历史工作间的复杂网络关系。

### 洞察结构的高级召回机制

Pulsara 的召回机制融合多重信号并输出结构化结果：

- **词法与语义检索** 实现基础信息的精准直达。
- **冲突伴随机制** 确保安全攸关的矛盾事实同步浮现，防患于未然。
- **多跳显式搜索** 深度探索相关证据、依赖项及记忆衍生路径。
- **轨迹记录** 全方位保障召回行为的评估、调试与复盘能力。
- **作用域与状态过滤** 阻断无效、隐藏或过期记忆进入错误上下文。

自动召回路径保持克制，避免每一轮都被记忆淹没；显式搜索路径则释放更强的语义探索能力，
让 Agent 在需要时沿着记忆图谱深入追踪。

## 核心特性矩阵

### 面向本地场景的工具流

Pulsara 深度集成文件系统操作、终端执行、长程进程轮询、大输出 artifact 解析与记忆检索。
真实工具调用会进入权限网关、事件日志与 inspector，而不是消失在模型回答背后。

### Skills 与 Capability Surface

Skills 在 Pulsara 中不是静态提示词堆叠，而是渐进式能力暴露的一部分。系统会按 turn 解析能力，
经策略网关评估，再把可用能力暴露给模型，并将相关决策写入运行时事实。

当某个 skill 指导 terminal call，或某项能力被隐藏、拒绝、批准、不可用时，系统都能留下清晰解释链。

### 谋定后动的 Plan Mode

针对高风险任务，Agent 可以先进入只读计划模式：探索代码库、提出结构化人工确认问题、出具计划草稿，
在获批后再稳健执行。Planning 深度融入权限控制与底层事件日志体系。

### Subagent Runtime

Pulsara 可以把有边界的子任务委派给 child agent runtime。Subagent 是拥有独立 event stream、
capability profile、生命周期与结果的 runtime session，而不是藏在父 prompt 里的函数调用。

父 Agent 会记录 subagent run 与 task 的图：

- `spawn_agent`、`wait_agent`、`stop_agent` 提供底层 run 控制。
- `create_agent_tasks`、`wait_agent_tasks`、`stop_agent_task` 提供 task-board 层，支持依赖与批量状态。
- child-only report tools 让 worker 主动报告 phase 与 explicit result。
- child result 会作为 internal evidence 进入父上下文，而不是伪装成用户消息。
- `list_agents` 与 inspector projection 可以展示 running、blocked、completed、consumed、delivered 等状态。

这让委派保持可审计：父 Agent 可以把 subagent report 当作次级证据，同时仍以 filesystem、terminal、
artifact 与 event log 这些可检查事实作为最高置信来源。

### 时间感知的工具观察

长程本地工作天然依赖时间。Pulsara 会将 tool observation timing 作为运行时事实记录到 tool result 中，
尤其适用于 terminal 与 `terminal_process` 观察。

模型可见的工具上下文可以包含：

- 这次观察发生在什么时候。
- 工具结果从何时开始、何时结束。
- observation duration。
- 输出是当前工具结果、后台进程观察、挂起/恢复交互，还是历史 replay。
- 终端领域自己的 timing，例如长程进程的运行时长。

这让模型能理解流式日志、后台训练任务、进程轮询、过期结果，以及“上一次观察之后发生了什么”，
而不必只从文本里猜时间。

### 丝滑的上下文交接

Pulsara 将 compaction 视为核心运行时事件，并生成持久化交接摘要。摘要清晰说明已经发生的事情、
后续执行方向、仍需关注的工具或进程，以及应该留存的关键事实。压缩失败会触发明确告警，避免静默状态污染。

## 极速启航

Pulsara 目前采用源码优先体验模式。克隆仓库，使用 `uv` 初始化环境，启动本地持久化服务即可进入 REPL。

```bash
git clone <your-pulsara-repo-url>
cd pulsara_agent

uv sync
docker compose up -d postgres oxigraph

cp .env.example .env
$EDITOR .env

uv run pulsara db migrate --env-file .env
uv run pulsara db verify --deep --env-file .env
uv run pulsara config-check --env-file .env
uv run pulsara host repl --env-file .env --workspace .
```

仓库自带的 Docker 环境只创建数据库管理员角色与受限 runtime 角色，不在容器初始化阶段创建 pgvector 或 Pulsara 表。
`pulsara db migrate` 是唯一 schema mutation owner：它使用 `PULSARA_POSTGRES_ADMIN_DSN` 安装 pgvector、执行不可变 migration、
补齐 runtime grants，并写入 durable migration ledger。

`PULSARA_POSTGRES_DSN` 必须绑定受限 runtime 角色。Host、Inspector、checkpoint 和 benchmark runtime 路径只做验证；数据库未迁移、
版本过旧或 catalog 漂移时，会在分配 session 资源之前 fail closed。V1 不接管没有 migration ledger 的旧 Pulsara 表；hard cut 时应同时
重置 PostgreSQL 数据库及其对应的 Oxigraph projection，再执行 migration。

在 REPL 中：

```text
pulsara> hello
pulsara> :help
pulsara> :plan
pulsara> :compact
pulsara> :close
```

利用命令行工具无缝恢复历史会话或审计运行时状态：

```bash
uv run pulsara host repl --env-file .env --workspace . --continue
uv run pulsara host repl --env-file .env --workspace . --list-sessions
uv run pulsara inspect health --env-file .env
```

## 配置

Pulsara 采用双模型角色设计：

- `pro` 负责主推理路径；
- `flash` 负责快速压缩、治理与辅助任务。

最小 `.env`：

```dotenv
PULSARA_API_KEY=sk-your-api-key
PULSARA_BASE_URL=https://api.openai.com/v1
PULSARA_PRO_MODEL=gpt-5
PULSARA_FLASH_MODEL=gpt-5-mini

PULSARA_POSTGRES_ADMIN_DSN=postgresql://pulsara_admin:pulsara_admin@localhost:5432/pulsara
PULSARA_POSTGRES_DSN=postgresql://pulsara_runtime:pulsara_runtime@localhost:5432/pulsara
PULSARA_OXIGRAPH_URL=http://localhost:7878
```

admin DSN 只由 `pulsara db migrate` 读取，不进入 runtime settings、wiring、日志或 Inspector 输出。可使用
`pulsara db status` 查看有界 migration 状态，使用 `pulsara db verify --deep` 执行离线完整 catalog 检查。

目前主路径面向 OpenAI-compatible Responses API。其他兼容提供商在 wire 行为匹配时也可以工作。

## 本地 Skills

Pulsara 可以从 Pulsara / agent skill 目录加载本地 skills，并把它们作为紧凑的路由提示暴露给模型。
当任务匹配某个 skill 时，Agent 可以用普通只读文件工具读取完整 `SKILL.md`，再使用终端或其他内置工具行动。

```bash
uv run pulsara skills status
uv run pulsara skills sync-bundled
uv run pulsara host repl --env-file .env --workspace . --skill hf-cli
```

Skills 是指导，runtime 是事实来源。Pulsara 会记录工具调用、权限决策、artifact 与 active-skill 归因。

## 当前状态

Pulsara 仍处于早期阶段，锋利、活跃，并快速演进。

它面向愿意运行本地 Python 项目、编辑 `.env`、启动 Docker 服务，并在需要时阅读 inspector 输出的 builder。
如果你追求真实工具执行、高级语义召回、可恢复长会话与极致可解释性，欢迎即刻登舰。

## 开发

```bash
uv run ruff check src tests
uv run pytest -q
```

可选的真实模型测试：

```bash
PULSARA_RUN_REAL_LLM=1 uv run pytest -m real_llm
```

## License

Pulsara 使用 [MIT License](LICENSE) 发布。
