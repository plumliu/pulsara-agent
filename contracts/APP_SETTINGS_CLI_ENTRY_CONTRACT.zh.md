# App Settings / CLI / REPL Entry Contract

_Created: 2026-07-04_

本文档定义 Pulsara 应用入口层契约：环境配置如何加载、CLI 子命令如何映射到 Host/Runtime、REPL 如何处理 plan/approval/resume/compaction，以及 bundled skills 管理如何与 runtime capability surface 分离。

相关代码：

- [src/pulsara_agent/settings.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/settings.py)
- [src/pulsara_agent/cli.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/cli.py)
- [src/pulsara_agent/repl.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/repl.py)
- [src/pulsara_agent/host/identity.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/host/identity.py)
- [tests/test_settings.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_settings.py)
- [tests/test_cli_host.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_cli_host.py)
- [tests/test_agent_runtime_loop.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_agent_runtime_loop.py)

---

## 1. 核心立场

CLI / REPL 是 product entry layer，不是第二套 runtime。

它必须：

- 构造 `PulsaraSettings`；
- 解析 workspace / permission / model role；
- 调用 `HostCore` / `InspectorService` / skill management API；
- 把 pending approval / pending plan interaction 渲染成用户可操作 UI；
- 在用户 detach 时保留 durable conversation；
- 在用户 close 时关闭 durable conversation。

它不得：

- 绕过 `HostCore` 直接构造 production runtime；
- 提供 in-memory / durable backend switch；
- 把 root design docs 当作 runtime truth；
- 把 CLI inspect 当作 live debugger；
- 在普通 host run/repl 失败时吞掉 storage/config 错误。

---

## 2. Settings contract

### 2.1 Prefix 与 env-file

`PulsaraSettings` 默认读取 `PULSARA_*` 环境变量。

CLI 可用：

- `--env-file`
- `--override-env`
- `--prefix`

`.env` parser 规则：

- 空行和 `#` 开头的注释忽略；
- 支持 `export KEY=value`；
- 支持单/双引号包裹 value；
- 支持 value 后空白隔开的 inline comment；
- 无 `=` 的非空行必须报错；
- 默认不覆盖已有环境变量；
- `override=True` 才覆盖已有环境变量。

### 2.2 Production storage 必填

`StorageConfig` 的生产契约：

- `postgres_dsn` 不能为空；
- `oxigraph_url` 不能为空；
- 默认值存在，但显式空白字符串必须拒绝；
- redacted output 只报告 `postgres_dsn_set`，不得泄漏完整 DSN。

这与 production hard cut 对齐：默认生产路径是真 PostgreSQL + 真 Oxigraph + 真 UOW。

### 2.3 Redacted diagnostics

`PulsaraSettings.redacted_dict()` 只能输出可诊断但不泄密的信息：

- provider / API / base URL / model name 可以输出；
- API key 只能输出 `api_key_set`；
- Postgres DSN 只能输出 set/unset；
- retrieval embedding/rerank key 只能输出 set/unset；
- governance relatedness policy/config numbers 可以输出。

---

## 3. CLI command surface

Pulsara CLI 顶层命令：

- `config-check`
- `host run`
- `host repl`
- `host inspect`
- `inspect run|session|artifact|memory|health`
- `skills sync-bundled|status|reset`

Removed / prohibited:

- `demo-ledger`
- `--in-memory`
- `--durable`
- workspace kind alias `ephemeral`

`project` 与 `transient` 是唯一 workspace kinds。

---

## 4. Permission CLI contract

`--permission-mode` 是主路径：

- `read-only`
- `ask-permissions`
- `accept-edits`
- `bypass-permissions`

高级 raw axes：

- `--permission-profile`
- `--approval-policy`
- `--terminal-access`

规则：

- `--permission-mode` 与 raw axes 互斥。
- `host run` / `host repl` 默认 `bypass-permissions`。
- `host inspect` 默认 `read-only`。
- 环境变量 `PULSARA_PERMISSION_MODE` 可以提供 run/repl 默认 mode。
- plan mode 激活后，REPL `:mode` 不得绕过 read-only plan invariant；该边界由 HostSession enforce。

---

## 5. Host run contract

`pulsara host run <prompt>` 是 one-shot HostCore driver。

规则：

- 启动前 best-effort sync bundled skills；
- 通过 `HostCore.open_session(...)` 打开 session；
- 传入 workspace、model role、permission policy；
- `--skill` 只对该 turn 激活 active skill；
- run 完成后关闭 HostSession，并 `close_conversation=True`；
- 最后 shutdown HostCore；
- 如果出现 pending approval / pending interaction，返回 JSON summary，不在 one-shot CLI 里继续交互。

`host run` 不得：

- 直接调用 `AgentRuntime`；
- 因 bundled skill sync 失败而阻止 run；
- 使用 in-memory backend flag；
- 自动批准 pending tool call。

---

## 6. Host REPL contract

`pulsara host repl` 是 durable conversation UI。

启动规则：

- 启动前 best-effort sync bundled skills；
- 普通启动创建新 HostSession；
- `--resume <runtime_session_id>` resume 指定 durable runtime session；
- `--continue` resume 当前 workspace 下最新 resumable session；
- `--list-sessions` 只列 session，不进入 REPL；
- `--resume` 且未显式传 workspace 时，使用 manifest workspace；
- `--continue` 没有 session 时必须输出友好错误，不裸抛 `KeyError`。

Detach / close：

- Ctrl-D、`:q`、`quit`、`exit` 是 detach，不关闭 durable conversation；
- `:close` 显式关闭 durable conversation；
- REPL finally 必须 shutdown HostCore。

Prompt：

- pending approval -> `approval> `
- pending plan interaction 或 active plan -> `plan> `
- normal -> `pulsara> `

`prompt_toolkit` EINTR/SIGCONT 情况必须 retry，而不是崩溃退出。若 stdin 非 TTY，使用 synchronous `input()` fallback。

---

## 7. REPL command contract

REPL commands：

- `:sessions`
- `:resume <session-id>`
- `:continue`
- `:close`
- `:status`
- `:mode <preset>`
- `:plan [reason]`
- `:interaction`
- `:choose <n|label>`
- `:answer <text>`
- `:approve-plan`
- `:revise-plan <feedback>`
- `:cancel-plan`
- `:force-exit-plan`
- `:approval`
- `:approve`
- `:deny`
- `:stop`
- `:compact`
- `:help`

Plan approval tokens in pending exit：

```text
approve, yes, 是, 好, 可以, 同意, 好的, 批准, y, Y
```

只做 strip 后 exact match；不做自然语言批准。

Plan question：

- `:choose <n|label>` 选择结构化 option；
- 裸数字/label 在 pending question 下等同选择；
- `:answer <text>` 若 exact match option label，必须设置 `selected_option`；
- free text disabled 时，非 option answer 必须拒绝。

Plan exit：

- `:approve-plan` 接受并退出 plan mode；
- `:revise-plan <feedback>` 拒绝当前 draft，继续 plan mode，并要求模型重新提交 plan；
- `:cancel-plan` 放弃 pending draft 并退出 plan workflow；
- `:force-exit-plan` 在无 pending exit 时也退出 active plan mode。

---

## 8. Manual compaction UI contract

`:compact` 是手动 compact safe point。

规则：

- 成功 compact 打印 `context compaction completed: ...`；
- 无 eligible window 打印 skipped；
- 失败打印 `context compaction failed: ...`；
- REPL listener 只打印 completed/failed notices，不应在 `pulsara>` prompt 后异步插入 auto compact notice；
- run-end 后台 auto compact 不应存在；compact timing 由 compaction contract 约束。

---

## 9. Host inspect contract

`pulsara host inspect` 是 static workspace capability inspection，不是 live runtime session list。

规则：

- 默认 permission 是 read-only；
- 不 sync bundled skills；
- 可以构造 durable runtime wiring 以获得 registry/capability exposure；
- 必须关闭临时 runtime session；
- 输出 workspace、tools、capability surface、permissions、memory scopes、skills、bundled skill status。

禁止：

- 把空 `workspace_supervisors: []` 伪装成 live supervisor truth；
- 触发 real host session open；
- 修改 bundled skills。

---

## 10. Durable inspector CLI contract

`pulsara inspect ...` 直接调用 `InspectorService`。

Subcommands：

- `run`
- `session`
- `artifact`
- `memory`
- `health`

规则：

- 使用 `PostgresInspectorStore(settings.storage.postgres_dsn)`；
- `oxigraph_url` 来自 settings；
- 输出 JSON；
- `memory` 不存在时必须以 not found 错误退出，不能成功返回空报告；
- `artifact` 可按 `--max-chars` 控制 preview；
- `--include-payload` 是显式 opt-in。

---

## 11. Skills management CLI contract

`pulsara skills ...` 管理 bundled Pulsara skills。

规则：

- `sync-bundled` 可安装/更新 bundled skills 到 `PULSARA_HOME`；
- `status` 是 read-only，不应创建 skills 目录；
- `reset <name>` 只接受有效 bundled skill name；
- invalid name 必须 clean error；
- `host run/repl` 的 bundled sync 是 best-effort：失败只写 stderr warning，不阻塞主流程；
- `host inspect` 不做 bundled sync。

这些 CLI 管理命令不等同于 runtime active skill。Runtime skill visibility 仍由 unified capability surface 决定。

---

## 12. Runtime context prompt from entry layer

Host/Runtime 必须在 model system prompt 中注入当前 runtime context：

- workspace root；
- workspace kind；
- workspace mode；
- terminal current cwd；
- terminal workdir 必须留在 workspace root 内；
- read-only filesystem tools 可以读取 workspace 外 ordinary text files；
- terminal tool 的 cwd/workdir 边界与 read-only filesystem tools 的 read boundary 不同。

这条提示不是 permission enforcement；真正 enforcement 在 terminal policy / filesystem tool / permission gate。

---

## 13. 禁止事项

- 不允许 CLI 提供 in-memory production backend switch。
- 不允许恢复 `ephemeral` workspace-kind alias。
- 不允许 one-shot `host run` 自动处理交互式 pending approval。
- 不允许 `host inspect` 修改 workspace/bundled skills。
- 不允许 settings redacted output 泄漏 secrets。
- 不允许 `:mode bypass-permissions` 在 active plan mode 下破坏 read-only plan invariant。
- 不允许 auto compact 在 prompt 已显示后异步污染输入行。
- 不允许 skills management CLI 绕过 capability provider 直接把 skill body 注入模型。

---

## 14. 测试守护

最低测试门槛：

- storage config 拒绝空 Postgres DSN / Oxigraph URL。
- env-file parser 支持 quotes/export/comment，默认不覆盖现有 env。
- config-check 输出 redacted settings。
- host run 使用 HostCore、thread workspace/model role/permission/active skill。
- host run pending approval/interaction 输出 JSON summary。
- removed commands/flags/kinds 被 argparse 拒绝。
- permission mode 与 raw axes 互斥。
- run 默认 bypass，inspect 默认 read-only。
- REPL approval/plan/question/choose/revise/cancel commands 正确转移状态。
- REPL prompt 在 pending approval/plan/active plan 下显示正确。
- REPL resume/continue/list/close/detach 语义正确。
- `--continue` 无 session 时友好报错。
- Ctrl-C 清 input 不关闭 session；SIGCONT/EINTR prompt retry。
- bundled skill sync best-effort；status read-only；reset invalid name clean error。
- host inspect 输出 static workspace capability snapshot 且不 sync bundled skills。
- runtime context prompt 包含 workspace root/kind、terminal cwd 和 filesystem/terminal boundary。
