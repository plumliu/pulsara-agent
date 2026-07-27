# Runtime Semantic Graph / Durable Projection Contract

_Created: 2026-07-04_
_Hard-cut: 2026-07-25 — durable projection jobs V1_

本文档冻结 EventLog runtime facts 到 timeline、tool-result evidence、artifact/graph relation及
外部materialization surface的长期边界。

相关实现：

- [runtime/projection_jobs/](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/projection_jobs)
- [runtime/publisher.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/publisher.py)
- [graph/postgres.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/graph/postgres.py)
- [memory/foundation/run_timeline_query.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/foundation/run_timeline_query.py)
- [tests/test_durable_projection_postgres.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_durable_projection_postgres.py)
- [tests/test_durable_projection_host_dogfood.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_durable_projection_host_dogfood.py)

相关契约：

- [EVENT_LOG_STORAGE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/EVENT_LOG_STORAGE_CONTRACT.zh.md)
- [RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/RUNTIME_EVENT_PUBLISHING_HOOKS_CONTRACT.zh.md)
- [GRAPH_JSONLD_STORAGE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/GRAPH_JSONLD_STORAGE_CONTRACT.zh.md)
- [ARTIFACT_STORE_CONTRACT.zh.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/contracts/ARTIFACT_STORE_CONTRACT.zh.md)

## 1. Authority

Canonical EventLog是runtime fact的唯一权威。Runtime semantic graph是可恢复derived
projection，不是第二条event truth。

每个durable projection job必须绑定：

- immutable migration-owned kind activation与seed contract；
- exact committed source event reference；
- `source_horizon.through_sequence == source_event_reference.sequence`；
- stable target key、handler contract与job semantic fingerprint；
- immutable cutover和canonical ledger-prefix checkpoint。

Seeder扫描页的high-water只属于seed checkpoint，不进入单个job semantic。Publisher只做
O(1) wake；wake丢失后periodic seeder仍须从durable checkpoint继续。

## 2. Durable job lifecycle

Job lifecycle由PostgreSQL repository唯一拥有：

```text
pending/retry_wait
  -> leased(generation, token, expiry)
  -> succeeded | superseded | retry_wait | dead_letter
```

要求：

- 同一target至多一个active handler lease；
- retry delay确定、有界且可从durable state恢复；
- cancellation和Host close只detach waiter，不删除owner；
- settlement UNKNOWN分类为`FULL | NONE | CONFLICT | UNRESOLVED`；
- applied与superseded均写immutable result receipt；
- target head只引用receipt，不依赖process-local callback outcome；
- dead-letter repair是typed CAS action并保留immutable repair chain；
- job failure只写job/diagnostic state，不递归生成AgentEvent。

Handler registry是closed static binding，不允许运行时dynamic import。Projection使用
process-owned executor与独立`PROJECTION_MAINTENANCE` PostgreSQL lane，不能挤占critical
EventLog writer reserve。

## 3. Run timeline

Timeline kind使用`full_replacement` target policy，target为
`runtime_session_id + run_id`。较新trigger可替换同一run的旧projection；乱序旧job必须得到
superseded receipt，不能回退target。

Timeline handler从上一applied target head的frontier开始，对
`(head_sequence, trigger_sequence]`做paged fold。它禁止：

- 从run sequence 1反复重建全部history；
- 读取trigger之后的event；
- 以16,384 events或16 MiB作为整个合法run的第二窗口；
- 因seed page/base选择改变persistent vector root。

Timeline result由immutable persistent pages/leaves、manifest/root、bounded summary、
`RunTimelineRecord` graph document、V2 canonical mutation与result receipt组成，并在同一
settlement UOW内提交。普通query、working-context和Inspector通过paged manifest读取；只有
显式bounded export可materialize完整timeline。

## 4. Tool-result execution evidence

Evidence kind使用`single_assignment` target policy。Target key只覆盖：

```text
runtime_session_id + run_id + tool_call_id
```

result semantic fingerprint不进入target key。同一tool call：

- exact same ToolResultEnd replay只exact-confirm；
- 第二个distinct terminal source无论sequence更高与否均产生target authority conflict；
- single-assignment target不产生superseded result。

Handler必须exact join：

- tool call start/end；
- tool result start/end；
- terminal projection；
- call ID、tool name、state、result semantic fingerprint；
- artifact semantic references。

ToolResult/Artifact/Turn document ID和timestamp由source semantic确定，不使用UUID或wall-clock
生成semantic identity。

## 5. Immutable graph relations

以下owned predicates只能通过immutable graph relation port写入：

- `Turn --rt:produced--> ToolResult`
- `ToolResult --rt:provides--> Artifact`

每条edge是独立content-addressed fact与PostgreSQL
`graph_relation_facts` row。并行tool-result jobs任意commit顺序不得覆盖共享Turn或Artifact
document。

`PostgresGraphStore.put_jsonld()`拒绝携带owned predicate的ordinary document。读取分为：

- `get_jsonld_read_view()`：typed bounded relation view；
- paged relation query；
- legacy `get_jsonld()`：只读合成base document与immutable relation rows。

Oxigraph surface按同一lowering contract把relation row降为exact direct quad。Relation
PostgreSQL row、Oxigraph quad与read-merge accumulator必须一致。

旧`ExecutionEvidenceLedger.record_tool_result*()`、`_record_turn_produced()`及同步Agent
persistence hook已删除。Canonical memory candidate ledger不拥有runtime evidence投影。

## 6. Canonical mutation V2

Derived graph/artifact documents与governed memory mutation使用immutable
`canonical_mutations_v2` base row；每个search/vector/Oxigraph目标拥有独立surface delivery
state。

Base mutation与surface delivery分离：

- producer在canonical UOW内原子写document/domain mutation、immutable mutation和初始surface
  states；
- surface worker按surface-local ordinal/predecessor持有lease；
- external I/O不持PostgreSQL row transaction；
- lost settlement可由stable delivery identity恢复；
- newer mutation不能跳过或自动supersede旧delta；
- search/vector/Oxigraph共用同一lease/retry/dead-letter contract。

Legacy `memory_write_outbox`只可由v6 migration decoder读取。生产replay hook、vector special
worker、direct Oxigraph mutation和V1 payload writer均已删除。

## 7. Working context与read side

Working context是recent activity cache，不是memory，也不是timeline authority。它只从已提交
timeline projection生成，写`working_context_summaries`，并保留
`do_not_write_back=true`。Timeline projection缺失时显示pending/unavailable，不能扫描当前
process scratchpad补造。

Runtime semantic read side包括：

- paged timeline query与bounded summary；
- Inspector durable projection；
- working-context projection；
- relation-aware JSON-LD view；
- external surface health。

EventLog仍是run存在与terminal state的最终authority。Projection lag不能把run误报为不存在。

## 8. Cutover与recovery

Schema v5安装job/receipt/head/lease、runtime write admission和immutable relation tables；v6
hard-cut mutation V2并安装timeline/evidence pre-activation contracts；v7/v8分别激活timeline与
evidence。

Activation前必须在database maintenance epoch内：

1. 停止normal write admission并drain in-flight owners；
2. 写immutable per-session coverage pages/receipts；
3. 验证heads未越过frozen horizon；
4. 原子安装activation/cutover与新normal epoch。

Migration runner是prerequisite-aware state machine。缺legacy binding plan或coverage时返回
typed `PREPARATION_REQUIRED`并停在当前head。Final binary必须能以historical-head maintenance
binding从v4依次推进到v8；historical binding不能启动production Host。

Restart恢复pending/retry/expired lease。Pre-cutover output只从durable receipt/outbox authority
显示；没有durable carrier时Inspector必须报告`not_durably_observable`。

## 9. 禁止事项

- 不允许publisher subscriber执行DB/archive/Oxigraph/embedding I/O。
- 不允许projection handler扫描完整EventLog或读取trigger后event。
- 不允许failure hook替代durable retry owner。
- 不允许timeline/evidence再次注册production persistence hook。
- 不允许direct document RMW写`rt:produced`或`rt:provides`。
- 不允许surface handler持锁执行external I/O。
- 不允许current code自报activation、handler或legacy surface contract。
- 不允许derived projection写governed memory candidate或修改EventLog truth。

## 10. 测试守护

最低门槛：

- trigger horizon/page-independent identity与seed/checkpoint atomicity；
- lease race、expiry、retry、settlement loss、dead-letter/repair；
- full-replacement乱序supersession与single-assignment conflict；
- 100k-event paged timeline与incremental frontier；
- exact tool call/result/projection join；
- parallel immutable relation order与owned-predicate rejection；
- V2 surface predecessor、external settlement recovery与legacy hard cut；
- v4→v8 staged migration、coverage/cutover和historical golden stability；
- Host run不等待projection，restart后backlog继续并由Inspector显示exact source/receipt；
- architecture guards证明旧hooks/outbox/workers/evidence writer物理消失。

---

## 11. D4 Projection Module Ownership

Durable projection facts与pure contracts位于top-level`projection_jobs`，lower-layer port位于
`ports.projection_jobs`。PostgreSQL job/surface/seeder/worker implementation继续位于
`runtime.projection_jobs`。`storage`、`graph`、`memory`不得import concrete runtime implementation。

Canonical mutation拆成pure factory、PostgreSQL repository、mutation writer/transaction capability；
D3 event/job/result/schema fingerprint与运行语义不变。Runtime publisher仍只执行O(1) wake。
