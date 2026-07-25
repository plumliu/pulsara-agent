# Memory Governance Write / Durable Surface Delivery Contract

_Created: 2026-07-04_
_Hard-cut: 2026-07-25 — canonical mutation V2_

本文档定义governed canonical memory写入、governance UOW、runtime event outbox与canonical
mutation V2 surface delivery的长期边界。

相关实现：

- [memory/governance/executor.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/governance/executor.py)
- [memory/canonical/unit_of_work.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/memory/canonical/unit_of_work.py)
- [runtime/projection_jobs/mutation_writer.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/projection_jobs/mutation_writer.py)
- [runtime/projection_jobs/surface.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/projection_jobs/surface.py)
- [runtime/projection_jobs/surface_handlers.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/runtime/projection_jobs/surface_handlers.py)

## 1. 核心立场

Governed canonical memory只有一条写路径：

```text
candidate pool
  -> MemoryGovernanceEngine
  -> MemoryGovernanceExecutor.apply_decision()
  -> GovernanceWriteUnitOfWork
  -> canonical graph/document + decision
  -> immutable canonical mutation V2 + surface delivery states
  -> stable runtime event outbox candidates
  -> commit
```

不存在no-UOW、in-memory或direct Oxigraph production fallback。

## 2. Governance UOW

`MemoryWriteUnitOfWork`在一个PostgreSQL transaction内写：

- candidate claim/decision与canonical graph rows；
- lifecycle mutation；
- immutable `canonical_mutations_v2` row；
- ordered per-surface delivery states；
- governance runtime event outbox ticket；
- required event parent rows。

成功commit后，runtime event dispatcher把stable candidates交回唯一RuntimeSession writer；
surface workers独立异步apply。任一UOW异常整组rollback。

`CanonicalMemoryLedger`只负责typed memory candidate到canonical node的gate与写入，不拥有
tool-result evidence、timeline或artifact projection。

## 3. Governance safety gates

Executor必须保留：

- candidate missing/invalid、scope不允许、exact duplicate；
- supersede/contradiction target type/status/scope；
- relatedness allowlist与FULL availability；
- UOW内target document/revision exact re-read；
- replacement evidence；
- single-target destructive action；
- drift时downgrade/regovernance，不修改旧canonical node。

Async search/vector命中不是canonical target authority。Lifecycle validation只认同步
`graph_documents + memory_nodes/node_revision`。

## 4. Canonical mutation V2

V2 base mutation是immutable semantic intent；surface delivery是mutable operational state。
每个mutation冻结：

- graph/domain/operation identity；
- canonical document或typed maintenance operation；
- source authority fingerprints；
- ordered surface plan；
- mutation contract fingerprint。

每个surface delivery冻结：

- stable delivery identity；
- surface-local sequence与predecessor；
- handler/target compatibility contract；
- status、attempt、lease generation/token/expiry；
- bounded error、next retry与result receipt。

Surface只有：

- `search_index`
- `vector_index`
- `oxigraph`

Search/vector/Oxigraph共用同一claim/lease/retry/dead-letter contract。Producer不得为某个
surface建立私有队列或special claim。

## 5. External side effects

Worker固定采用三相：

```text
claim/prepare transaction
-> external I/O without PostgreSQL row lock
-> settlement transaction
```

External apply必须使用stable idempotency key。连接丢失后，settlement从exact delivery与
external receipt分类`FULL | NONE | CONFLICT | UNRESOLVED`；不得因callback outcome丢失而重做
不同semantic mutation。

较新mutation不能自动supersede旧delta。Surface-local predecessor不连续时停止claim并显示
authority diagnostic。

## 6. Runtime event outbox

`memory_governance_event_outbox`只拥有“memory UOW已经commit、runtime event尚待唯一writer
materialize”的stable candidate batch。它不是canonical mutation surface队列。

规则：

- event IDs/payload在UOW内冻结；
- dispatcher失败保留ticket并exact retry；
- EventLog FULL后按account推进；
- publisher failure不撤销memory UOW；
-不得由governance repository direct写EventLog。

## 7. Candidate projection outbox

`memory_candidate_projection_outbox`属于reflection/compaction producer provenance：

- producer event FULL与outbox rows同事务；
- dispatcher从confirmed producer event投影candidate pool；
- pending/failed row由session-owned owner恢复。

它不属于D3 canonical mutation surface delivery。Compaction memory extension的更高层owner仍按
独立契约管理。

## 8. Governance source evidence

Governance input只来自：

- `GovernanceSourceEvidenceSemanticFact`
- `GovernanceSourceEvidenceAttributionFact`
- `GovernanceEvidencePromptProjectionFact`

不得扫描整轮EventLog、读取raw model segment或从tool result prose猜provenance。
MAIN_AGENT_TOOL必须join accepted terminal projection、control disposition、tool call/pair/result与
canonical user span。Semantic、physical attribution与bounded prompt projection使用不同
fingerprint。

候选选择与durable claim同transaction。Preparation顺序固定：

```text
claim
-> atomic transcript authority snapshot
-> evidence/relatedness freeze
-> resolve exact model call
-> persist GovernanceBatchInputSnapshot
-> Prepared FULL
-> ModelCallStart
-> decisions/UOW
-> terminal governance batch
```

Recovery只可rebind frozen artifact/target/call，不能从当前配置重新resolve。

## 9. 禁止事项

- 不允许恢复`memory_write_outbox` production writer/replay hook。
- 不允许vector/Oxigraph special worker或direct mutation。
- 不允许external I/O持有canonical UOW row lock。
- 不允许surface failure回滚canonical memory commit。
- 不允许mutation或delivery contract由current composition自证。
- 不允许governance event dispatcher绕过RuntimeSession writer。
- 不允许candidate projection outbox与canonical mutation surface state混为一表。

## 10. 测试守护

最低门槛：

- governance graph/decision/mutation/event ticket同UOW；
- rollback不留下mutation或surface row；
- surface-local predecessor、lease race、retry/dead-letter/repair；
- external apply成功但settlement丢失可恢复；
- search/vector/Oxigraph handler contract与target compatibility；
- graph reset/delete产生same-UOW V2 maintenance mutation；
- legacy V1 rows只经typed v6 binding plan迁移；
- architecture guard证明旧outbox writer/replay/vector/Oxigraph worker物理消失；
- relatedness allowlist、target revision drift与destructive-action downgrade。
