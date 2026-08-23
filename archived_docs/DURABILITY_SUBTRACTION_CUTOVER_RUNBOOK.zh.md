# Pulsara durability subtraction：complete-reset 与旧 owner quiesce runbook

状态：**Stage 0 只读运行手册；未执行**

适用切换：Stage 2 production authority activation

迁移策略：reset-only，不提供旧 authority 导入、转换或反向投影

## 1. 目的与不可变边界

本手册定义 durability subtraction hard cut 前，如何让旧 Runtime 的 durable writer、后台 worker 与进程内 physical owner 安全退出，然后从空的 Pulsara-owned store 启动新 schema。它不属于 Stage 0/1 的生产执行路径，也不授权当前实施者执行 reset。

以下规则不可放宽：

- 切换期间不允许旧、新 authority 双写；
- 进程外副作用可能仍在运行时，只交给 operator 处理，不把它重新导入新 Runtime；
- complete reset 是唯一向前与回滚机制；
- EventLog `NONE` / `UNKNOWN` / `CONFLICT`、canonical corruption 和尚未物理退出的 owner 不能被当作成功；
- 任何数据库、blob、Oxigraph 或 presentation 清理命令都必须先在目标环境由 operator 明确复核。

## 2. 切换前证据与 go/no-go

Operator 必须先保存以下不含 secret 的证据：

1. 待部署 binary/image identity、目标 migration registry identity 与 Stage 2 gate report；
2. 当前 Host writer、projection worker、terminal monitor、subagent executor 和外部 process/effect inventory；
3. PostgreSQL canonical endpoint/database identity，但不保存 DSN、密码或 token；
4. Pulsara-owned blob namespace、derived index、Oxigraph graph 与 presentation store 的精确边界；
5. maintenance window、operator、开始时间与回滚决策人。

任一 physical owner 无法定位、无法 fence，或目标资源边界不明确时，必须 `NO-GO`。

## 3. Complete-reset / quiesce 顺序

### Step 1：停止新 admission

- 对外关闭新 Host session、resume、prompt/steer、tool dispatch 与 durable job admission；
- 等待 admission fence 可观察地生效；
- 不用 UI disconnect 或流量切走冒充 writer fence。

验收：没有新的 run、tool attempt、queue mutation 或 job claim 能在 fence 后开始。

### Step 2：fence 旧 writer 与 worker

- fence 每个 session 的旧 Host writer lease/generation；
- fence durable worker claim domain；
- 停止 terminal monitor 注册/触发；
- 停止 subagent activation/execution admission；
- 拒绝所有晚到的旧 generation mutation。

验收：旧 Host、worker、monitor 与 child executor 均不能获得新的 durable/physical work。

### Step 3：取消并 join 当前进程 physical owner

按现有 ownership contract 执行：

1. 停止 producer admission；
2. 请求 cooperative cancellation 或关闭该 owner 独占的输入/connection；
3. join model/tool/MCP/terminal/subagent/job、EventLog writer、checkpoint、presentation、audit 与 executor/thread/process owner；
4. physical exit 前保持 PostgreSQL pool、artifact store、Oxigraph、executor 和 terminal manager 可用；
5. deadline 到期即停止切换，不释放依赖、不伪造 `CLOSED`。

验收：进程内 owner registry 为空，且没有后台 task/thread 能再访问待清理资源。

### Step 4：隔离仍在外部运行的 process/effect

- 记录无法证明 terminal outcome 的外部 process/effect、remote operation ID 与 operator disposition；
- operator 选择等待、查询、终止或接受 `outcome_unknown`；
- 不在新 Runtime 中恢复 coroutine，不把旧 process、lease、candidate、receipt 或 checkpoint 导入新 schema；
- non-idempotent effect 不得自动重放。

验收：每个外部 effect 都有人工归属；没有“由新 Runtime 接管旧 owner”的路径。

### Step 5：清空 Pulsara-owned PostgreSQL schema/data

- 连接前重新核验 canonical endpoint、database OID 与 admin role；
- 使用届时 Stage 2 实施规格冻结的 reset command；
- 只清理 Pulsara-owned relation/schema，不触碰共享数据库的其他 owner；
- reset 后验证旧 migration ledger、EventLog、projection job、checkpoint、queue、continuation 和 presentation 数据均不存在。

本 Stage 0 手册故意不内嵌可直接复制执行的 destructive SQL。

验收：目标 Pulsara store 是空世界；不存在可被旧 binary 解释为可 resume 的 durable row。

### Step 6：清空 shared blob 与 derived plane

- 删除精确 Pulsara-owned blob namespace 中的旧 artifact；
- 清空 Oxigraph/search derived index；
- 清空 presentation history/root/checkpoint、UI cache 与其他 derived read model；
- 共享 store 必须按 namespace/owner 精确删除，禁止 bucket/database 级模糊清理。

验收：canonical row 不再引用旧 blob，derived plane 不再暴露旧 session 数据。

### Step 7：从 empty store 执行 Stage 2 migration

- 只运行经过 gate 的 Stage 2 migration registry；
- 执行 deep schema verify、restricted runtime-role privilege verify 与 empty-world bootstrap；
- 启动一个 writer 和独立 worker fencing domain；
- 在开放 admission 前完成 fresh-session text/tool/close/reopen smoke。

验收：schema、registry、privilege 与 writer/worker fencing identity 全部匹配部署证据。

### Step 8：开放 admission 并观察

- 先开放一个受控 canary session；
- 确认只产生新 conversation kernel authority，不出现旧 EventType/owner/dual-write；
- 确认 crash 后表现为 interruption/conversation rehydrate，而非 execution replay；
- canary 通过后再逐步开放普通 admission。

### Step 9：rollback 只能再次 complete reset

一旦 Stage 2 写入新 schema，不允许原地启动旧 binary，也不允许把新 row 转换回旧 EventLog graph。Rollback 顺序仍是：

1. 停止新 admission；
2. fence/join 新 writer、worker 与 physical owner；
3. operator 处理外部 effect；
4. 再次清空全部 Pulsara-owned PostgreSQL/blob/derived state；
5. 从空 store 部署选定版本。

## 4. 明确禁止的迁移捷径

本 hard cut 不提供：

- import/cold reader；
- old-to-new converter；
- event-to-row identity map；
- reverse projection；
- online shadow write或兼容性双写；
- 保留旧 checkpoint/receipt/lease 供新 Runtime 查询；
- 用 blanket reconciliation 清除、skip/xfail 或人工改行伪造 clean cutover。

## 5. Operator 回执模板

回执只记录安全的身份与结果：

```text
cutover_id:
operator:
binary_or_image_identity:
target_registry_identity:
admission_fenced_at:
old_writer_worker_fence_result:
physical_owner_join_result:
external_effect_operator_dispositions:
postgres_empty_world_verified:
blob_and_derived_planes_cleared:
stage2_migration_and_deep_verify_result:
canary_result:
final_disposition: activated | aborted_before_reset | rolled_back_by_complete_reset
```

不得在回执中记录 DSN、数据库密码、provider key、MCP secret、prompt/tool 私密正文或用户环境绝对路径。
