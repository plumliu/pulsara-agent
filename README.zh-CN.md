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
├── canonical conversation runner
├── tool policy + Host-scoped physical tools
├── exact-four durable job executor
├── PostgreSQL-only memory
├── process-local live event bus
└── Terminal Protocol v3 gateway
        └── Go terminal client

PostgreSQL
├── pulsara_v3：24 张产品关系
├── selective agent_events occurrence journal
├── public.vector capability
└── public.pulsara_schema_migrations（只保存 universe metadata）
```

Durable 边界有意保持狭窄：

- canonical relational rows 拥有 conversation、tool、job、memory 与
  coordination 的当前语义真值；
- closed 27-type `agent_events` journal 只记录 accepted occurrence，不用于恢复
  execution；
- 23 种 live event 只存在于内存，进程退出即可丢失；
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

- OpenAI-compatible Responses 与 Chat Completions transport；
- filesystem、todo、`terminal`、`terminal_process`、`terminal_monitor` 与
  scoped `artifact_read` tools；
- 真实 PIPE/PTY Terminal output streaming、exact process-local cursor、typed
  GAP、same-Host future monitor observation，以及 provider safe-point 上的
  autonomous continuation；
- default-deny subprocess environment、bounded login-shell snapshot、最近
  `.venv/bin`、foreground cwd continuity，以及 Host close 时的 physical
  process-group drain；
- 通过 shared blob store 保留完整 sanitized tool output：中等输出完整
  展示并附 artifact reference，大输出使用 UTF-8-safe head/tail preview
  并按需有界读取；
- bounded Host-scoped subagent；
- bundled/local skills；
- PostgreSQL FTS/vector memory，以及显式 direct、reverse 与最多 two-hop
  relation traversal；
- 四类具名 durable background job：compaction、post-compaction memory
  extraction、memory governance、memory-index refresh；
- canonical Inspector 与 Protocol v3 terminal observation。

当前 conversation kernel 未安装 MCP execution adapter。若配置了 enabled MCP
server，Host 会 fail closed，不会静默忽略。

## 环境要求

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- PostgreSQL，且 `public.vector >= 0.5.0`
- 仅在构建可选 Terminal client 时需要 Go

## 初始化

```sh
uv sync
```

环境文件至少需要：

```dotenv
PULSARA_API_KEY=...
PULSARA_BASE_URL=https://api.openai.com/v1
PULSARA_API=openai_responses
PULSARA_PRO_MODEL=...
PULSARA_FLASH_MODEL=...

PULSARA_POSTGRES_DSN=postgresql://pulsara_runtime:...@localhost:5432/pulsara
PULSARA_POSTGRES_ADMIN_DSN=postgresql://pulsara_admin:...@localhost:5432/pulsara
```

Admin DSN 只用于 `db migrate`；普通 Host 只验证 clean schema，并使用 runtime
role。

```sh
uv run pulsara db migrate --env-file .env
uv run pulsara db verify --deep --env-file .env
```

唯一 active migration universe 是
`pulsara.conversation-kernel.v1` generation 1，从 version 0 开始。Round 1 与
Round 2 在保持 exact 24 张产品关系的同时更新了 version-0 `tool_results` 与
canonical Terminal-observation 契约；当前 baseline/catalog identity 与验证证据记录在
[`round1_tool_output_artifact_activation.json`](benchmarks/suites/core/v1/round1_tool_output_artifact_activation.json)
和
[`round2_terminal_runtime_activation.json`](benchmarks/suites/core/v1/round2_terminal_runtime_activation.json)。旧 v13
数据库只会得到 `schema_migration_universe_reset_required`，不会被在线导入、
翻译或升级。请严格遵守
[clean-baseline runbook](STAGE_5_CLEAN_BASELINE_RUNBOOK.zh.md)，没有针对 exact
endpoint/database 的 operator 授权时，不得重置真实环境。

## 运行

单轮：

```sh
uv run pulsara host run \
  --env-file .env \
  --workspace /path/to/project \
  "解释这个仓库"
```

交互式 REPL：

```sh
uv run pulsara host repl \
  --env-file .env \
  --workspace /path/to/project
```

恢复该 workspace 最近一次 conversation：

```sh
uv run pulsara host repl \
  --env-file .env \
  --workspace /path/to/project \
  --continue
```

只读检查 composition：

```sh
uv run pulsara host inspect \
  --env-file .env \
  --workspace /path/to/project
```

## Terminal client

构建 Protocol v3 Go 客户端：

```sh
cd clients/terminal
mkdir -p bin
go build -trimpath -o bin/pulsara-tui ./cmd/pulsara-tui
cd ../..

uv run pulsara host tui \
  --env-file .env \
  --workspace /path/to/project \
  --tui-binary "$PWD/clients/terminal/bin/pulsara-tui"
```

Python 拥有 canonical state、command、policy、secret 与 recovery；Go 只拥有
rendering 与 interaction。Protocol v2 与 Presentation Foundation 已完全退役。

## 开发门控

```sh
uv run ruff check .
uv run pytest -q
uv run python -m compileall -q src tests
uv run python tools/generate_terminal_protocol_contract.py --check
(cd clients/terminal && go test ./... && go vet ./...)
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
