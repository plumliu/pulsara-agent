# Pulsara Renderer-neutral Client Boundary 实施记录

> 状态：ACTIVATED
>
> 决策日期：2026-08-20
>
> 删除前可恢复checkpoint：`a3131c5b636fb3a354c5770ca70409b974eb4095`（已推送`origin/main`）

## 1. 裁决

Pulsara不再发布或维护bundled Go/TUI。未完成的terminal client不是长期兼容面，也不值得继续让Python Runtime、测试矩阵和发布流程为它承担双语言成本。

删除client不等于删除client boundary。以下能力继续保留：

- renderer-neutral Protocol v3 protobuf schema；
- Python generated binding；
- canonical row/live owner到wire DTO的bounded adapter；
- Unix-domain Protocol v3 Gateway及typed command admission；
- snapshot、history、Live GAP/resync、content read与Plan/TODO/subagent control语义；
- Protocol schema identity、golden fixture与Python conformance tests。

未来Web或Desktop client只能消费这些typed事实并提交Gateway允许的command，不能拥有canonical conversation truth、permission、recovery、tool attempt或Live delivery truth。

## 2. 物理删除面

以下路径整体删除：

~~~text
clients/terminal/
src/pulsara_agent/terminal_client/
tests/test_stage2_tui_cross_language.py
~~~

这同时删除：

- Go module、generated Go protobuf、Bubble Tea model与kernel client；
- Go/TUI S0 spike、binary与平台探针；
- Python binary resolution、process supervision、bootstrap与launcher；
- `pulsara host tui`、`--tui-binary`与`--clear-scrollback`；
- 只输出静态composition preview、容易被误认成canonical Inspector的`pulsara host inspect`；
- Go build/test/vet/module verification gate。

`host run`与`host repl`保持不变。REPL是开发/诊断入口，不重新升级为未来主要UI；canonical observation由未来client经typed query与Protocol边界承接，不保留另一个名为Inspector的静态preview产品面。

## 3. 保留面

当前wire真源继续是：

~~~text
src/pulsara_agent/terminal_protocol/schema/terminal_kernel_v3.proto
src/pulsara_agent/terminal_protocol/generated_v3/terminal_kernel_v3_pb2.py
src/pulsara_agent/terminal_protocol/canonical_v3.py
src/pulsara_agent/terminal_protocol/v3_gateway.py
tools/generate_terminal_protocol_contract.py
~~~

协议generator只生成并校验Python binding，不要求第二语言generator或仓库外toolchain。frontend hard-cut同时从proto移除已无consumer的语言专属package option，并显式刷新Protocol v3 schema fingerprint、Python generated binding与wire golden；消息、field number与序列化payload保持不变。这是一次可审计的renderer-neutral identity更新，不保留指向已删除client路径的schema metadata。

## 4. Authority与恢复边界

Protocol v3仍然只是transport/projector：

- canonical row回答已经接受的conversation/control事实；
- process-local owner回答当前Host的Live状态；
- Gateway执行scope、role、command identity与permission admission；
- client只持有可丢失的rendering、selection、draft和local navigation state；
- disconnect后client从snapshot与Live baseline重新建立视图，不靠client cache修复Runtime；
- client delivery失败不得否定canonical commit、阻塞Host close或创建receipt/replay job。

未来Web/Desktop client若需要HTTP/WebSocket transport，可以在Protocol v3 semantic DTO之外增加transport adapter；不得复制一套canonical query、自由JSON command vocabulary或durable UI projection。

## 5. 不变量与oracle

本次删除：

- 不修改PostgreSQL schema；
- 不新增或删除Committed/Live event type；
- 不改变subject、append guard、product relation或durable job数量；
- 不修改Protocol v3 wire message、field number或payload encoding；
- 仅因删除语言专属package metadata而显式刷新一次schema fingerprint、Python binding与golden；
- 不改变Kernel、Tool、Memory、Plan、MCP、TODO或Subagent authority。

因此closed oracle保持：

~~~text
31 Committed events
24 Live events
13 subjects
2 append guards
26 product relations
1 durable job
~~~

## 6. 验收

至少证明：

1. 仓库不存在tracked/untracked Go source、`go.mod`、`go.sum`或`clients/terminal`；
2. Python production import graph不存在`pulsara_agent.terminal_client`；
3. CLI不再暴露TUI入口；
4. Protocol v3 Python generation、schema identity与golden fixture仍通过；
5. Protocol snapshot/control/GAP测试继续通过；
6. architecture gate明确禁止Go client与Python launcher重新出现；
7. Ruff、compileall、全量pytest、`uv lock --check`与`git diff --check`通过。

Activation验证结果：

~~~text
targeted Protocol/architecture/skill regression: 52 passed
full pytest: 765 passed
Ruff: passed
compileall: passed
Protocol v3 Python generator/check: passed
uv lock --check: passed
git diff --check: passed
Go source/module files in worktree: 0
real-provider dogfood: passed (OpenRouter Chat Completions, 2 fresh processes,
  abrupt Host A after canonical commit, fresh Host B continuation, ephemeral
  clean-v0 PostgreSQL force-dropped, no private content or credentials recorded)
~~~
