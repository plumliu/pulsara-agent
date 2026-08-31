# Pulsara Local Web Application Lifecycle Hard-Cut Implementation Specification

状态：Active（2026-08-30）

本规范定义 Pulsara 本地 Web 应用的唯一启动、连接、会话激活与关闭路径。它补齐
`KernelHostCore`、Terminal Protocol v3 与浏览器 UI 之间缺失的应用控制面，但不改变
conversation kernel 的 canonical authority、provider input continuity、现有 durable vocabulary
或进程内 live owner。

本规范参考 deepseek-harness 的单一 launcher、提交后发布、cold-safe read、连接代际与
quiescent dispose 原则；不复制其 Cordis 插件树、配置 HMR 或 session event-log authority。

## 1. 产品结果

用户执行一个命令：

```text
pulsara app
```

该命令负责完整的本地应用生命周期：

```text
load config
  -> verify local runtime dependencies
  -> start KernelHostCore
  -> start Terminal Protocol v3 on a private Unix socket
  -> start same-origin loopback HTTP application
  -> commit READY
  -> print/open the bare loopback browser URL
  -> serve until shutdown
  -> stop browser admission
  -> detach protocol clients
  -> close protocol listener
  -> close live HostSessions without closing canonical conversations
  -> shutdown KernelHostCore
  -> stop HTTP listener
  -> STOPPED
```

浏览器不需要 OpenAI、ChatGPT 或任何第三方账号登录。模型 provider 的 API key 仍只属于
Pulsara 进程配置；它既不是浏览器登录凭据，也不得发送到浏览器。

## 2. 唯一 owner 与状态机

### 2.1 Application owner

`LocalWebApplication` 是唯一根 owner。它持有：

- 一个 `KernelHostCore`；
- 一个 `LocalSessionController`；
- 一个 `TerminalKernelProtocolServer`；
- 一个 loopback HTTP server；
- 所有 browser connection；
- 根关闭任务与 ready 提交点。

状态为：

```text
NEW -> STARTING -> READY -> DRAINING -> STOPPED
                 \-> FAILED
```

约束：

- `READY` 只能提交一次；
- HTTP port 成功 bind 不等于 `READY`；
- URL 只能在所有关键 owner 启动成功且静态前端可读取后发布；
- 启动失败必须逆序回收已经创建的 owner，不发布伪 ready URL；
- `aclose()` memoize 为一个任务，所有调用者 join 同一次物理回收；
- `DRAINING` 后拒绝 create、resume、connect 与新 mutation；
- Application shutdown 不把任何 canonical conversation 标记为 closed。

### 2.2 HostSession owner

`LocalSessionController` 维护 canonical `session_id -> HostSessionHandle` 的进程内映射。
Handle 携带 `KernelHostSession` 与创建者专有的 close capability；普通查询不得获得或伪造
close 权限。

激活策略：

| 操作 | cold-safe | 是否创建/恢复 HostSession |
| --- | --- | --- |
| list sessions | 是 | 否 |
| app/runtime health | 是 | 否 |
| create session | 否 | 是，完成 setup 后发布 |
| connect/open existing session | 否 | 无 live owner 时恢复；并发恢复合并 |
| snapshot/history/observe | 否 | 通过已建立 attachment |
| prompt/steer/stop/plan/interaction | 否 | 要求 controller attachment |
| browser disconnect | 不适用 | 只 detach attachment，不关闭 HostSession |
| explicit unload HostSession | 不适用 | quiescent close；canonical conversation 保持 OPEN |
| explicit close conversation | 不适用 | 仅显式用户动作可请求 canonical close |

创建/恢复在以下动作全部成功前不得进入 live map：workspace resolution、writer acquisition、
Hook/Plugin/MCP setup、`KernelHostSession.start_mcp()`。失败路径 join 现有 Kernel 回滚，不保留
半发布 session。

应用支持且只向创建界面暴露两种工作目录来源：

- **快速开始**：Application 在 Pulsara home 下的受管 workspace 根创建唯一目录，再以
  `transient` Kernel workspace kind 打开会话。这里的 `transient` 只表示它不拥有 project
  memory scope；目录和会话都是持久的，Application/HostSession close 不删除该目录。受管目录
  basename 使用 `quick-YYYYMMDD-HHMMSS-<8 hex>`；8 位随机后缀发生碰撞时通过原子建目录
  重新生成，不得再写入完整 UUID 或长 hash；
- **指定目录**：用户提供一个已经存在的本地绝对目录，Application 以 `project` workspace
  kind 打开会话。

canonical `sessions` row 同事务记录 workspace kind、规范化绝对 root 与 display label。它们是
跨应用重启恢复 HostWorkspaceInput 所需的最小当前事实；不增加 relation、event、job 或 replay
authority。创建后目录不存在、不可读或 row 元数据与恢复请求不一致时，恢复必须 typed fail，
不得静默换到启动目录。快速开始只在 session 创建提交失败且目录仍为空时回收本次尚未发布的
新目录；已发布目录永不自动删除。

同一 canonical session 的并发 resume 由一个 process-local promise 合并。该 promise 不是
durable job、lease、receipt 或 recovery authority。

### 2.3 Protocol attachment owner

Terminal Protocol v3 继续是 Kernel 与 renderer 之间唯一数据平面。Local Web bridge 不直读
Kernel 私有字段来重建 transcript，也不创建第二套 command semantics。

每个 browser runtime connection 在服务端拥有一个用于 snapshot/history/command 的 attachment，
以及一个独立的 observation attachment：

- 通过私有 Unix socket 连接；
- launch capability 只存在于 Python 进程内，不发送到页面 JavaScript；
- 首帧执行 controller/observer `HelloRequest`；
- 后续 snapshot、history、observe、command、query、content 与 interaction 请求原样经过
  Protocol v3；
- protobuf 到 JSON 的翻译保持 proto field name 与 enum name，不派生 durable truth；
- browser connection close 时先发送 `DETACH`，再关闭 Unix stream；物理断线也由 gateway
  finally 释放 live observer、live-control subscriber 与 controller slot。

controller ownership **按 canonical session 隔离**，绝不是 Application 全局单例：

- 两个不同 session 可以同时各自拥有 controller、HostSession 与 active turn，互不关闭、并行执行；
- 同一个 session 的第一个 browser connection 获得 controller，后续 connection 以 observer
  稳定连接，不得驱逐现有 controller；
- observer 可以持续读取 transcript、reasoning、tool、TODO 与子任务进展，但 UI 不展示发送、
  stop、Plan、permission、compaction、interaction resolution 或接纳子任务结果等 mutation control；
- 用户在 observer 页面显式选择“在此窗口继续”时，connection 请求携带 `takeover=true`。Bridge
  只替换该 session 的旧 browser controller；其他 observer 与其他 session 的 controller 均保持不变；
- 被替换的旧页面按普通断线重连后成为 observer，不得自动夺回 controller，因此不能形成两个页面
  互相驱逐、无限重连的 ping-pong。

同一个 browser runtime connection 的数据 attachment 与 observation attachment 共同构成一个
不可拆分的连接 generation。任一 attachment 关闭、到达 EOF 或报告 typed transport failure 时，
Bridge 必须原子移除整个 generation，释放该 session 的 browser controller 映射并关闭另一条
attachment。仍存活的 observation 长轮询不得掩盖已经死亡的数据通道，也不得让页面继续显示为
可操作；下一次请求进入统一重连路径，用新 snapshot 和 live-control baseline 恢复界面。

### 2.4 Browser connection generation

浏览器 adapter 的状态为：

```text
starting -> ready -> reconnecting -> ready
                  \-> offline/failed
```

每次成功 connect 都得到新的 opaque `connection_id` 与递增的 process-local generation。
generation 只有在 `HelloAccepted`、canonical snapshot 与 live-control baseline 全部取得后才对
UI 可见。

物理断线、`STALE_ATTACHMENT` 或任一成对 attachment 失效后：

1. 丢弃旧 generation 的全部 live draft、interaction 与 TODO projection；
2. 新建 attachment；
3. 用完整 canonical snapshot 与 live-control snapshot replacement 发布新 generation；
4. 从返回 cursor 继续 observation。

不跨 Host replacement 恢复 coroutine，不把 live ring 当 durable replay，不要求收到断线期间
的每一个 process-local increment。

## 3. 本地浏览器访问边界

应用只监听 `127.0.0.1`，并直接发布裸地址：

```text
http://127.0.0.1:<port>/
```

当前产品不做浏览器鉴权：根页面、静态资源和 API 均不要求 token、cookie、账号或浏览器身份。
任何能够访问该进程 loopback listener 的本机客户端都处于同一访问边界；这是当前本地单用户
产品明确接受的约束，不建立 durable identity store，也不保留旧 capability exchange 的兼容路径。

仍然保留以下请求边界：

- 所有请求必须通过 exact `Host` 检查；
- mutation 还必须通过 exact `Origin` 与 `Sec-Fetch-Site` 检查；
- 不信任 `X-Forwarded-*`；
- 服务只 bind `127.0.0.1`，不得因为移除鉴权而改为监听 LAN 或任意网卡；
- Protocol launch capability 与 `PULSARA_API_KEY` 仍不得进入前端 state、localStorage 或响应 JSON。

## 4. 应用控制面

控制面使用同源 JSON HTTP；数据面 payload 是 Protocol v3 的无损 JSON projection。

最小路由：

```text
GET    /api/app/bootstrap
GET    /api/sessions
GET    /api/sessions/{session_id}/tasks?limit={1..50}&cursor={opaque}
POST   /api/sessions
POST   /api/sessions/{session_id}/connections
DELETE /api/connections/{connection_id}
POST   /api/connections/{connection_id}/snapshot
POST   /api/connections/{connection_id}/history
POST   /api/connections/{connection_id}/observe
POST   /api/connections/{connection_id}/command
POST   /api/connections/{connection_id}/query-command
POST   /api/connections/{connection_id}/live-control-snapshot
POST   /api/connections/{connection_id}/resolve-interaction
POST   /api/connections/{connection_id}/resolve-plan-interaction
POST   /api/connections/{connection_id}/read-content
```

connection 创建默认不携带 body；显式接管同一 session 时只允许：

```json
{"takeover":true}
```

创建请求只有以下两种合法形态：

```json
{"workspace_kind":"quick"}
```

```json
{"workspace_kind":"project","workspace_path":"/absolute/existing/directory"}
```

请求不携带 prompt、Plan 或 permission。响应提交 session summary 及其 workspace；浏览器随后
建立 connection，第一轮目标由普通 composer command 发送。

控制面只负责：workspace/session selection、cold list、create/resume dedupe、attachment 创建、
lifecycle ownership。它不得重新解释 command outcome；特别是
`PENDING` 仍不是成功。

所有应用错误使用 typed JSON envelope：

```json
{
  "error": {
    "code": "STABLE_CODE",
    "message": "safe public text",
    "retryable": false
  }
}
```

HTTP cancellation只取消该 HTTP waiter；已经被 Kernel 接受的 command 必须通过同一个
`command_id` 查询，不能由 bridge 自动重发。

## 5. 前端 hard cut

production UI 启动后只使用 `LocalHttpRuntimeAdapter`。原 demo fixtures 只允许存在于 Story/
test fixture，不得作为应用 fallback；连接失败必须显示明确的 local runtime 错误与重连动作，
不能显示模拟“成功”。

首屏必须等待 bootstrap，然后：

- 展示 cold session list；没有 session 时呈现真实 empty state；
- 打开 session 时 create/resume attachment，并以 snapshot replacement 建立 transcript；
- 成功 create/resume/reconnect 后重新读取 session list；当前页面连接、进程内已载入 HostSession
  与仅可恢复的 durable session 必须分别投影，不得把 cold list cache 当成持续状态；
- observation 的 immutable entry append、current control replace、live delta 与三类 GAP 处理
  遵守 Protocol v3；
- 切换 session 时先关闭旧 connection；组件 unmount 与页面 unload 尽力 detach；
- active turn 下 Enter 发 `SUBMIT_PROMPT` 形成 future queue；显式 steer 必须携带 exact active
  `turn_id`；
- composer 的 Plan 选择与 permission mode 都是下一次发送的 renderer-local draft。Plan
  发送按 `ENTER_PLAN` 成功后 `SUBMIT_PROMPT` 的顺序执行；permission 只进入该次 admission；
- optimistic echo 只作为带 `command_id` 的 renderer-local pending item，并在 canonical
  acceptance、rejection 或 connection generation replacement 时退休；
- stop、Plan、compaction、permission 与 interaction 不再由 timer 或本地 reducer伪造。

没有 production contract 的数据与操作不得进入用户可见 DOM；不得用 disabled control、
“不可用”提示、空卡片或 demo records 暗示已经存在对应能力。已有真实契约的总览、会话和
本地设置只展示其实际可读或可操作的数据。

子任务属于会话，不设独立任务页。`GET /api/sessions/{session_id}/tasks` 从 durable task/result/
dependency rows 做 cold-safe keyset pagination，不创建或恢复 HostSession，也不新增 durable job、
event 或恢复权威。浏览器读完当前会话的完整清单后，以任务 scope 隔离 ROOT 与 worker transcript、
tool result 和 process-local live draft；durable terminal state 必须覆盖迟到的 live fragment。
当前会话检查器与主对话原位块共享合并投影，逐项展示目标、依赖、真实状态、补充消息、工具活动、
Markdown 结果和已有 controller result acceptance，不允许把内部 tool API 名称直接当作产品文案。

## 6. 关闭与信号

普通关闭顺序：

1. Application 进入 `DRAINING` 并停止 HTTP API admission；
2. 取消/等待所有长轮询 waiter；
3. detach 并关闭全部 browser Protocol client；
4. 关闭 Protocol v3 listener 与升级/HTTP keep-alive connections；
5. 逐个关闭 live HostSession，`close_conversation=False`；
6. `KernelHostCore.shutdown()` join 所有 session 与物理 owner；
7. 清除 Unix socket；
8. Application 进入 `STOPPED`。

第一下 SIGINT/SIGTERM 启动同一个 graceful `aclose()`。第二下信号是用户明确的强制进程退出
请求。Application 不另设自动总倒计时：多个 HostSession 的既有 physical close boundary 已分别
拥有明确 owner，额外的进程总时限会把并发/会话数量意外变成新的 lifetime cap。

## 7. 不增加的 authority

本 hard cut 不新增：

- durable event kind、subject slot、append guard、product relation 或 durable job；
- browser receipt/checkpoint/reducer/replay recovery；
- application/session lifetime 总上限；
- provider SYSTEM/tools rebase 边界；
- capability fingerprint registry 或 browser-side authority；
- OpenAI identity、OAuth 或远程账号依赖。

## 8. 激活证据

实现完成必须通过：

1. 单元测试：ready 只在完整启动后发布；partial start 逆序回滚；`aclose()` 合并；
2. 访问边界测试：裸 loopback URL 无鉴权可用、无 token/cookie/redirect、Host/Origin/CSRF
   拒绝、无 secret JSON；
3. 控制面测试：cold 全局 list 不恢复 session；快速开始目录持久；指定目录校验；跨工作区并发
   resume 合并；disconnect 不 canonical close；
4. Protocol 测试：真实 Unix socket hello/snapshot/observe/command/detach；launch capability 不出
   Python 进程；
5. 前端测试：无 demo fallback、empty/error/reconnecting、snapshot replacement、live delta、GAP、
   optimistic command retirement，以及 session-scoped durable task pagination、批次/依赖、全部终态、
   live overlay、原位定位、结果接纳、工具结果与 Markdown 投影；
6. `uv run pytest` 的相关 Python suites、`npm run lint`、`npm test`、`npm run build:local`；
7. 本地 dogfood：`pulsara app` 只输出 ready 后裸 URL，全新浏览器无需 token、cookie 或账号即可
   用“快速开始”和“指定目录”创建会话，并可 open/send/Plan/permission/stream/stop/reload；真实模型创建并等待多个
   子任务时，浏览器可目视观察每个任务的实时工具活动、补充消息、依赖等待和最终结果；刷新后
   完整清单仍在，进程退出后 Unix socket 与 listener 均消失，两类 canonical session 均可再次
   resume，但 child coroutine 不自动续跑。
