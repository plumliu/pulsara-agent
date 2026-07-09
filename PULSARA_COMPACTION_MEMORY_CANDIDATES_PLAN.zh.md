# 从 Context Compaction 中提取记忆候选的设计方案

## 0. 结论

Pulsara 现有 memory 架构已经基本支持“auto-compaction 顺手提取记忆候选，但正式落盘仍由 governance 决定”这个方向。

最重要的边界是：

```text
ContextCompactionService / compact LLM
  只负责压缩上下文，并可额外产生 memory candidate proposals
  不写 canonical memory graph
  不影响下一轮 memory recall

CandidatePool
  负责持久化 pending candidates
  保留 compaction provenance
  仍不是正式语义记忆

MemoryGovernanceEngine / MemoryGovernanceExecutor
  唯一负责 submit / skip / merge / supersede / contradict
  唯一能通过 MemoryWriteService 写入 canonical graph

GraphStore / Postgres / Oxigraph
  只承载 governance 接受后的 canonical memory nodes / relations / indexes
```

这个设计适合捕捉“项目习惯”类信息，例如：

> 在 pulsara_agent 项目中，用户经常希望 main 分支提交后，将 src/ 和 tests/ 同步到 release 分支，在 release 上提交，并 push 到 GitHub。

它应该先成为 project-scoped `Preference` candidate，而不是直接成为 durable memory，更不能直接变成自动执行规则。

## 1. 当前代码真相

### 1.1 Context compaction 现在只产 summary artifact

当前 compaction 主路径在：

- `src/pulsara_agent/runtime/compaction/service.py`
- `src/pulsara_agent/runtime/compaction/prompts/context_compaction_prompt.md`
- `src/pulsara_agent/runtime/compaction/planner.py`

`ContextCompactionService.compact()` 当前流程是：

1. 从 event log 构造 `CompactionPlan`。
2. append `ContextCompactionStartedEvent`。
3. 调 compact LLM。
4. `strip_compaction_analysis(raw_summary)` 提取 `<summary>`。
5. 将 summary 存为 artifact，metadata 中有 `do_not_write_back=True`。
6. append `ContextCompactionCompletedEvent`。

当前 prompt 明确写了：

```text
Do NOT write any compact-summary content as a durable memory candidate.
```

这个规则在当时是正确的：它防止 compact summary 被误当成长期记忆。但如果我们要做“候选提取”，需要把它改成更精确的边界：

```text
Do not write durable memory.
You may emit structured memory candidate proposals.
These are pending candidates only; governance decides whether to persist them.
```

### 1.2 CandidatePool 已经是正确的“候选，不是正式记忆”入口

当前候选池在：

- `src/pulsara_agent/event/candidates.py`
- `src/pulsara_agent/memory/candidates/pool.py`

`PooledMemoryCandidate` 当前字段包括：

```python
entry_id
payload
origin
source_session_id
source_run_id
source_turn_id
source_reply_id
source_tool_call_id
user_quote
created_at
```

`CandidateOrigin` 当前只有：

```python
MAIN_AGENT_TOOL
REFLECTION
GOVERNANCE
```

候选池文档已经明确：

> Candidate pool is an append-only inbox for proposed durable memories. It is not canonical semantic memory.

这正好符合 compaction candidate 的定位。

### 1.3 Governance 已经是唯一 canonical write path

当前治理路径在：

- `src/pulsara_agent/memory/governance/engine.py`
- `src/pulsara_agent/memory/governance/executor.py`
- `src/pulsara_agent/memory/canonical/write_service.py`
- `src/pulsara_agent/memory/canonical/ledger.py`
- `src/pulsara_agent/memory/canonical/write_gate.py`

关键语义：

- `MemoryGovernanceEngine.run_pending()` 只读取 candidate pool 中的 pending candidates。
- Governance LLM 只输出 decision，不直接写 graph。
- `MemoryGovernanceExecutor.apply_decision()` 通过 `MemoryWriteService` 和 ledger 写正式 graph。
- 正式写入后才产生 `MemoryCandidateProposedEvent` / `MemoryWriteResultEvent` / `MemoryWriteFailedEvent`。

这意味着 compaction 只要 append candidate pool，就能天然进入现有 governance。

### 1.4 Graph schema 支持正式 memory，但不应该存 pending candidate

当前 schema 在：

- `src/pulsara_agent/storage/memory_schema.py`

正式 memory 相关表：

```text
graph_documents
memory_nodes
memory_relations
memory_write_outbox
memory_search_index
memory_vector_index
recall_traces
recall_usages
```

candidate/governance 相关表在 `memory_candidates` / `memory_governance_decisions`，由 `CANDIDATE_POOL_SCHEMA_SQL` 定义。

所以 schema 分层是对的：

```text
memory_candidates              pending / audit candidate
memory_governance_decisions    governance audit
memory_write_outbox            canonical write propagation
memory_nodes / graph_documents canonical memory
```

Oxigraph 只需要接收 governance 后的 canonical mutation outbox，不需要接收 pending compaction candidates。

### 1.5 scope 体系已经适合项目习惯

当前 scope 在：

- `src/pulsara_agent/memory/scope.py`
- `src/pulsara_agent/host/identity.py`

已有：

```text
ctx:user
ctx:workspace/<workspace-hash>
graph:user/<memory_domain_id>
```

对于“我经常让你先同步代码到 release，再 push GitHub”这类习惯，默认应该是：

```text
scope = ctx:workspace/<pulsara_agent workspace hash>
```

不应该默认写入 `ctx:user`，因为它明显是当前项目工作流偏好。

## 2. 目标语义

### 2.1 Compact LLM 是观察者，不是裁判

冻结语义：

```text
compaction candidate extraction is best-effort observation
not durable memory write
not memory recall
not user confirmation
```

compact LLM 可以提出：

```json
{
  "kind": "Preference",
  "statement": "In this workspace, the user often wants main-branch changes committed and then synchronized to release before pushing to GitHub.",
  "reason": "Observed repeated workflow across compacted runs.",
  "confidence": "medium"
}
```

但它不能直接产出：

```text
accepted memory
active memory node
recall projection
automatic behavior rule
```

### 2.2 Governance 是唯一接受者

候选进入 pool 后，仍必须经过 governance：

```text
pending candidate
  -> governance submit_as_is / skip / merge / correct
  -> MemoryWriteGate
  -> canonical graph write
  -> search/vector/Oxigraph materialization
```

如果 governance 认为候选过弱、重复、过度泛化或 scope 不安全，应 `skip`。

scope / source authority / verification status 不能由 compact LLM 自由决定。V1 由 runtime 统一强制：

```text
scope = current workspace scope
source_authority = conversation_evidence
verification_status = inferred
evidence_ids = []
```

这避免 compact LLM 把项目习惯错误升级成 `ctx:user` 全局偏好，或者把模型观察伪装成用户确认。

### 2.3 Candidate 不应立刻污染下一轮上下文

pending compaction candidate 不参与：

- memory recall
- working context projection
- context compiler memory projection
- subagent profile
- permission/gate decision

只有 canonical memory accepted 后，才进入 recall/projection。

## 3. 数据模型修改

### 3.1 CandidateOrigin 增加 COMPACTION

在 `src/pulsara_agent/memory/candidates/pool.py`：

```python
class CandidateOrigin(StrEnum):
    MAIN_AGENT_TOOL = "main_agent_tool"
    REFLECTION = "reflection"
    COMPACTION = "compaction"
    GOVERNANCE = "governance"
```

pending 规则保持：

```python
candidate.origin != CandidateOrigin.GOVERNANCE
```

也就是说 compaction candidates 和 reflection candidates 一样，默认进入 pending governance。

### 3.2 PooledMemoryCandidate 增加 provenance metadata

建议给 `PooledMemoryCandidate` 增加：

```python
source_event_id: str | None = None
source_artifact_id: str | None = None
intent_fingerprint: str | None = None
metadata: dict[str, Any] = Field(default_factory=dict)
```

Postgres schema 增加：

```sql
ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS source_event_id TEXT;

ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS source_artifact_id TEXT;

ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS intent_fingerprint TEXT;

ALTER TABLE memory_candidates
    ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_memory_candidates_session_origin_fingerprint
    ON memory_candidates(source_session_id, origin, intent_fingerprint)
    WHERE intent_fingerprint IS NOT NULL;
```

这个 index 只是 pending 去重查询的辅助索引，不是 unique constraint。`still pending` 需要结合 `memory_governance_decisions` 判断 terminal decision，不能用永久唯一约束表达；否则已被 governance skip / submit 的旧候选会阻止未来重新提出同一意图。

compaction candidate 的 metadata 至少包含：

```json
{
  "source": "context_compaction",
  "compaction_id": "context_compaction:...",
  "trigger": "auto",
  "reason": "preflight_context_threshold",
  "window_id": "context_window:...",
  "window_number": 3,
  "through_sequence": 1234,
  "keep_after_sequence": 1190,
  "included_run_ids": ["run:..."],
  "included_artifact_ids": ["..."],
  "summary_artifact_id": "context_compaction_...:summary",
  "summary_excerpt": "bounded excerpt from the generated compaction summary...",
  "summary_excerpt_chars": 1234,
  "summary_excerpt_truncated": false,
  "source_event_id": "event-id-of-context-compaction-completed",
  "source_event_sequence": 1235,
  "intent_fingerprint": "sha256:...",
  "candidate_extractor_version": "compaction-memory-candidates:v1"
}
```

`source_event_id` 指向已经成功写入 event log 的 `ContextCompactionCompletedEvent`。后续的 `ContextCompactionMemoryCandidatesProposedEvent` 只是 audit carrier，不是候选来源真相。

metadata 必须 bounded。`included_run_ids`、`included_artifact_ids` 这类 provenance 列表只能保存有限 sample，并额外保存 `included_run_count`、`included_artifact_count`、`included_run_ids_truncated`、`included_artifact_ids_truncated` 等解释字段。因为 governance snapshot 会包含 candidate model dump，不能让 provenance 列表把治理 prompt 撑大。

`summary_excerpt` 也必须 bounded，并在 append candidate 时由 `ContextCompactionService` 从已经解析成功的 `<summary>` 中裁剪得到。V1 不给 `MemoryGovernanceEngine` / `MemoryGovernanceExecutor` 新增 archive reader 依赖；governance 需要的 clipped summary evidence 直接从 candidate metadata 读取。完整 summary 仍以 `summary_artifact_id` 指向 artifact store，但治理 prompt 只使用 bounded excerpt。

### 3.3 不要把 event id 直接塞进 candidate.evidence_ids

这是重要边界。

`MemoryCandidate.evidence_ids` 最终会进入 ledger，ledger 会要求这些 id 是 graph 中存在的 Evidence nodes。Event log id 不是 Evidence node id。

因此 V1 规则：

```text
candidate.evidence_ids = []
event ids / compaction ids / artifact ids live in PooledMemoryCandidate.metadata
```

后续如果需要正式 evidence graph node，可以另做：

```text
evidence:context_compaction:<compaction_id>
```

但不要在 V1 为了候选提取强行创建 Evidence node。

## 4. Compaction 输出格式

### 4.1 推荐输出 summary + optional candidates block

当前 compaction prompt 要求：

```xml
<analysis>...</analysis>
<summary>...</summary>
```

建议扩展为：保留必需的 `<analysis>` 与 `<summary>`，并允许可选的 `<memory_candidates_json>`。

```xml
<analysis>
...
</analysis>

<summary>
...
</summary>

<memory_candidates_json>
{
  "candidates": [
    {
      "kind": "Preference",
      "statement": "...",
      "reason": "Observed repeated workflow across compacted runs.",
      "confidence": "medium"
    }
  ],
  "skipped": [
    {
      "statement": "...",
      "reason": "one-off task detail"
    }
  ]
}
</memory_candidates_json>
```

`<summary>` 仍是唯一会进入 compaction summary artifact 的内容。

`<memory_candidates_json>` 只由 runtime parser 读取并转换为 candidate pool entries。该 block 缺失不是 diagnostic，除非本次 `ContextCompactionMemoryCandidatePolicy` 明确要求尝试 extraction。普通 compaction 输出只有 `<analysis>` + `<summary>` 仍然合法。

如果输出里出现 `<memory_candidates_json>`，则必须同时有合法 `<summary>`。V1 不允许 “tagless summary + candidate block” 走 legacy fallback，因为那会把 JSON 候选块写入 summary artifact 并在后续 replay 中污染模型上下文。

parser contract 必须保持顺序：

```text
1. 先按现有规则严格解析 <summary>。
2. <summary> 缺失、空、malformed -> compaction 失败，保持现有 fail-closed 语义。
3. 只有 summary 成功后，才 best-effort 解析 <memory_candidates_json>。
4. candidates JSON malformed / partially invalid -> compaction 仍成功，仅产生 diagnostic / skipped count。
```

也就是说，“candidate parse failure 不影响 compaction”只适用于 summary 已经成功解析的情况。

parser 输出必须是 Pulsara-owned DTO，而不是把 compact LLM 的 raw dict 继续传给 sink：

```python
@dataclass(frozen=True, slots=True)
class CompactionCandidateDiagnostic:
    code: str
    field: str | None = None
    message: str = ""
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class CompactionCandidateSkippedItem:
    code: str
    reason: str
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class NormalizedCompactionCandidate:
    payload: CandidatePayload
    intent_fingerprint: str
    raw_index: int


@dataclass(frozen=True, slots=True)
class CompactionCandidateParseResult:
    attempted_count: int
    candidates: tuple[NormalizedCompactionCandidate, ...]
    skipped: tuple[CompactionCandidateSkippedItem, ...]
    diagnostics: tuple[CompactionCandidateDiagnostic, ...]
```

secret-like raw text 只能在 parser 内部短暂存在。跨 parser 边界后，`skipped` / `diagnostics` 必须已经脱敏。

这些 parser DTO 属于 runtime compaction 内部。任何进入 `AgentEvent` schema / serialization 的 DTO 必须定义在 `pulsara_agent.event` 层或独立低层 event DTO 模块，避免 `event/events.py` 反向 import runtime compaction parser。

### 4.2 V1 候选类型建议只允许 Preference

V1 最安全的 compaction extraction 类型：

```text
Preference
```

原因：

- “项目开发习惯”天然是 preference。
- `Preference` 不要求 Evidence node 才能写入 active graph。
- `source_authority=conversation_evidence` 不会被 `MemoryWriteGate.evaluate_preference()` 直接拒绝。

V1 不建议 compaction 直接产：

- `Claim`：没有 Evidence node 时会变成 needs_review 或被治理跳过。
- `Observation`：更容易是一次性过程事实。
- `Decision`：没有明确用户/系统 authority 时容易过强。
- `ActionBoundary`：容易变成过度自动化规则，例如“总是 push release”，风险更高。

如果 compact LLM 输出非 Preference，V1 parser 应该：

```text
drop with diagnostic
or normalize to skipped candidate metadata
```

不要让它直接进入 pool。

### 4.3 source_authority / verification_status 固定降级

compaction 不是用户显式说“请记住”，所以 V1 统一写：

```python
source_authority = memory.SourceAuthority.CONVERSATION_EVIDENCE
verification_status = memory.VerificationStatus.INFERRED
```

即便 compact LLM 输出 `explicit_user_instruction`，parser 也应该降级，除非未来实现能严格证明 compacted events 中存在用户明确 memory instruction。

### 4.4 scope 由 runtime 强制，不接受 LLM 选择

若 `memory_domain.workspace_kind == "project"`：

```text
scope = ctx:workspace/<current workspace key>
```

V1 忽略 compact LLM 输出的 `scope` 字段。即使 LLM 输出 `ctx:user`，runtime 也必须强制改为当前 workspace scope，或直接 skip 并记录 diagnostic。不要只校验 `allowed_write_scopes`，因为 project workspace 的 allowed scopes 同时包含 `ctx:user` 和 workspace scope。

若是 transient workspace：

```text
do not emit compaction memory candidates by default
```

V1 不支持 transient compaction candidates。未来如果要支持 `ctx:user`，必须另做显式用户指令证明链，而不是相信 compact LLM 的 scope 判断。

## 5. Runtime 接入点

### 5.1 ContextCompactionService 需要一个 candidate sink

当前 `ContextCompactionService` 构造参数只有：

```python
event_log
archive
llm_runtime
runtime_session_id
policy
model_role
```

建议新增一个可选 collaborator，而不是直接把 governance engine 塞进去：

```python
@dataclass(slots=True)
class ContextCompactionMemoryCandidateSink:
    candidate_pool: CandidatePool
    memory_domain: MemoryDomainContext
```

更推荐直接定义为 protocol，避免 sink 拥有任何治理调度能力：

```python
@dataclass(frozen=True, slots=True)
class CompactionCandidateAppendResult:
    source_event_id: str
    source_event_sequence: int
    source_artifact_id: str
    entry_ids: tuple[str, ...]
    duplicate_count: int = 0
    skipped: tuple[CompactionCandidateSkippedItem, ...] = ()
    diagnostics: tuple[CompactionCandidateDiagnostic, ...] = ()


class CompactionMemoryCandidateSink(Protocol):
    def append_compaction_candidates(
        self,
        *,
        completed_event: ContextCompactionCompletedEvent,
        summary_artifact_id: str,
        parse_result: CompactionCandidateParseResult,
    ) -> CompactionCandidateAppendResult: ...
```

`ContextCompactionService` 不应该知道 `MemoryGovernanceEngine`，否则 compaction 会越权。

sink 也不应持有裸 `governance_notifier`。V1 冻结 `HostSession` 作为治理唤醒 owner：`ContextCompactionService` 只负责写 compaction / candidate audit facts 并返回结果，不主动调度 governance。`HostSession` 根据 stored/published compaction candidate event、trigger、reason、`metadata.phase` 和 same-turn cutoff barrier 统一决定是否 notify。未来如果要下沉到 service，必须使用明确 trigger-aware 的接口，例如：

```python
def schedule_governance_after_compaction(
    *,
    trigger: str,
    phase: str | None,
    source_event_id: str,
    source_event_sequence: int,
) -> None: ...
```

这可以防止实现者在 sink append 成功后无条件 notify governance，或让 service 与 HostSession 重复 notify，从而绕过 preflight “不污染本轮上下文”的屏障。

`completed_event` 必须是已经由 event log append 返回的 stored event，带稳定 `id` 和 `sequence`。candidate 的 `source_event_id` / `metadata.source_event_sequence` 使用这个 stored `ContextCompactionCompletedEvent`，不是 started event，也不是后续 proposed event。

候选的 `source_run_id` / `source_turn_id` / `source_reply_id` 只是 candidate pool 当前 FK/storage attribution。由于 `ContextCompactionService` 当前用被压缩窗口最新 event 的 context 写 compaction events，这些字段不代表候选的完整 evidence source。compaction candidate 的真实 evidence window 必须以 `metadata.compaction_id`、`through_sequence`、`included_run_ids`、`summary_artifact_id` 和 bounded evidence view 为准。

### 5.2 Wiring 层注入

当前 `runtime/wiring.py::_with_memory_governance_engine()` 创建 `ContextCompactionService`。

这里可以根据是否有：

```python
runtime_wiring.candidate_pool
runtime_wiring.memory_domain
runtime_wiring.governance_coordinator
```

来注入 candidate sink。

不要让 sink 依赖 `runtime_wiring.memory_governance_engine`。真实代码中 `ContextCompactionService` 和 `MemoryGovernanceEngine` 在同一个 `_with_memory_governance_engine()` 返回值里同时构造，sink 此时不应要求一个尚未构造完成的 engine。V1 的 notify 由 HostSession 的 trigger-aware 调度层处理，或在 HostSession 完成 run 后继续使用现有 `_notify_governance()`。

如果是 in-memory/test wiring 或没有 memory_domain：

```text
compaction summary works
candidate extraction disabled
```

### 5.3 Extraction policy 是显式配置，不散落在 prompt/service/sink

`<memory_candidates_json>` 是否期待出现、何时提取、最多提取多少条，必须由一个明确的 policy 控制，而不是散落在 prompt 文案、parser 分支和 sink 行为中。

建议新增：

```python
@dataclass(frozen=True, slots=True)
class ContextCompactionMemoryCandidatePolicy:
    enabled: bool = True
    extract_on_manual: bool = True
    extract_on_preflight: bool = True
    extract_on_mid_turn: bool = False
    missing_candidates_block_policy: Literal["ignore", "diagnostic"] = "ignore"
    max_candidates_per_compaction: int = 3
    max_summary_excerpt_chars: int = 2_000
    max_provenance_ids: int = 5
    extractor_version: str = "compaction-memory-candidates:v1"
```

它可以作为 `ContextCompactionPolicy.memory_candidates` 的嵌套字段，或作为 `ContextCompactionService` 的独立构造参数。无论放在哪里，runtime 行为都必须从这个 policy 读取。

V1 默认：

```text
manual compaction: extract
preflight auto compaction: extract, but delay governance visibility until triggering run finishes / cutoff barrier
mid-turn inline compaction: do not extract by default
max candidates per compaction: 3
missing candidates block: ignore
```

如果 `enabled=False`，不修改 prompt、不解析 candidate block、不写 zero-proposal audit event。如果 `enabled=True` 且当前 trigger policy 允许 extraction，`<memory_candidates_json>` 缺失是否作为 diagnostic 由 `missing_candidates_block_policy` 决定；V1 默认 `ignore`。普通未启用 extraction 的 compaction 仍只需要 `<analysis>` + `<summary>`。

### 5.4 解析失败不应让 compaction 失败

compaction 的主功能是缩上下文。候选提取是附加能力。

V1 规则：

```text
summary parsing fails -> compaction fails
candidate parsing fails -> compaction succeeds, candidate_count=0, diagnostic recorded
candidate validation partially fails -> valid candidates enter pool; invalid candidates counted/skipped
```

这避免“为了记忆候选，反而破坏 auto-compaction”。

这里的前提是 summary 已经成功按现有 `<analysis>/<summary>` 规则解析。新增 candidate parser 不能放宽 summary fail-closed 语义。

### 5.5 Governance notify/cutoff：不得污染触发 compaction 的同一 user turn

候选 append 成功后，不要在 sink 内直接调用裸 `governance_notifier()`，更不要 `await governance.run_pending()`。V1 中是否调度治理必须由 HostSession 的 trigger-aware 调度层决定；`ContextCompactionService` 不主动 notify governance。

当前 HostSession 在 `_finish_active_run()` 会 `_notify_governance()`。auto-compaction 发生在 preflight 时，下一次 run 结束也会触发 governance，所以即时 notify 不是强依赖。

建议：

```text
V1: append candidates, but preflight compaction-origin candidates must not affect the same user turn that triggered compaction.
Default implementation: delay governance notify until run finish / ordinary HostSession safe point.
Alternative implementation: add recall/projection generation cutoff barrier.
No governance wait in compaction path.
```

这是硬边界。preflight compaction 发生在当前 user turn 的模型调用前；如果它立刻 notify governance，并且 governance 很快接受 candidate，则同一 turn 的 recall/projection 可能看到刚刚由 compaction 提取的新 memory，破坏“pending candidate 不参与下一轮上下文”的承诺。V1 默认不做即时 preflight notify，依赖 `_finish_active_run()` 的治理 safe point。

mid-turn inline compaction 的 V1 语义更保守：

```text
manual compaction: may append candidates and best-effort notify governance after compaction completes
preflight auto compaction: may append candidates, but governance notify is delayed until the triggering run finishes
mid-turn inline compaction: may append candidates, but must not synchronously run governance; V1 may choose to disable candidate extraction entirely for phase="mid_turn"
```

如果实现选择 mid-turn 也提取候选，metadata 必须包含 `phase="mid_turn"`，governance notify 最多是 run-finish/safe-point wake，不得阻塞当前 active run，也不得影响当前 model loop 的 recall/projection。

### 5.6 Candidate append / proposed event 一致性规则

V1 不要求 candidate pool append 与 event log append 处在同一个数据库事务里。为了避免半真相，冻结如下规则：

```text
1. Summary artifact + ContextCompactionCompletedEvent 是 compaction 成功的事实源。
2. Candidate append 是 best-effort 附加路径。
3. ContextCompactionMemoryCandidatesProposedEvent 是 extraction/append audit event。
4. 如果本次有成功 entry，必须先完成 candidate pool commit，再写 proposed event。
5. 如果本次没有成功 entry，但 extraction 已被尝试，且 parser / validation / secret filter / duplicate detection 产生 skipped、duplicate 或 diagnostics，应该写 proposed_count=0、candidate_entry_ids=[] 的 proposed event。
6. Proposed event 写失败不使 compaction 失败。
7. Inspector 必须能通过 memory_candidates.metadata.compaction_id / source_event_id 反查 orphan candidates。
8. Proposed event 里只能引用已经成功进入 pool 的 entry_ids。
9. 如果部分 candidate append 失败，event proposed_count 只统计成功 entry；失败项进入 sanitized diagnostics。
```

append failure diagnostics 不得写 `str(exc)`。持久化 event / inspector 中只允许稳定 `code`、`error_type` / exception class name、候选 index 等脱敏字段；SQL、路径、raw candidate、secret-like 文本都不得进入 diagnostic message。

反过来，不允许先写 proposed event 再尝试 append candidates。否则 event 会引用不存在的 candidate entries。

如果 candidate extraction 未启用，或者输出中没有 `<memory_candidates_json>` 且没有任何 parser diagnostic，可以不写 proposed event。否则 proposed event 是本次 extraction/append 的 audit carrier。

如果未来要求强一致，必须引入同事务 unit-of-work，把 candidate pool append 和 proposed event append 合并到一个持久化边界；V1 不假装已经有这种一致性。

### 5.7 Pending candidate 去重 / idempotency

compaction 可能反复从 carried-forward summary 或相似窗口里提出同一句候选。V1 需要 append-level idempotency，至少避免 pending pool 无限堆重复。

建议计算：

```text
intent_fingerprint = sha256(origin + scope + kind + normalized_statement + extractor_version)
```

append 策略：

```text
if source_session_id + origin=compaction + same intent_fingerprint is still pending:
  skip append
  record diagnostic duplicate_pending_compaction_candidate
else:
  append candidate
```

`still pending` 必须由 append 逻辑查询 candidate pool + governance terminal decisions 得出。不要把 `(source_session_id, origin, intent_fingerprint)` 做成永久 unique constraint；该 index 只用于加速 pending duplicate lookup。

governance 仍然保留 canonical duplicate 判断，但不要把所有重复都推迟到 governance。

## 6. Event / inspect 设计

### 6.1 新增 typed event 是唯一 audit event

建议新增：

```python
class ContextCompactionMemoryCandidatesProposedEvent(EventBase):
    type: Literal[EventType.CONTEXT_COMPACTION_MEMORY_CANDIDATES_PROPOSED]
    compaction_id: str
    source_event_id: str
    source_event_sequence: int
    summary_artifact_id: str
    candidate_entry_ids: list[str]
    attempted_count: int = 0
    proposed_count: int
    skipped_count: int
    duplicate_count: int = 0
    error_count: int = 0
    extractor_version: str = "compaction-memory-candidates:v1"
    diagnostics: list[CompactionCandidateDiagnostic] = Field(default_factory=list)
```

这里的 `CompactionCandidateDiagnostic` 必须是 event-visible DTO，定义在 `pulsara_agent.event` 层或等价的低层 event DTO 模块。runtime parser 可使用自己的内部 DTO，但写 event 前必须转换成 event-visible DTO，避免事件层依赖 runtime compaction 模块。

它应写在 `ContextCompactionCompletedEvent` 之后。

这个 event 不代表 memory accepted。它表示本次 compaction candidate extraction 已经完成；`proposed_count > 0` 时也表示这些 `candidate_entry_ids` 已成功进入 candidate pool，`proposed_count == 0` 时则用于承载全 skip / malformed candidate JSON / 全 duplicate 等 sanitized audit diagnostics。

不要保留“或者给 `ContextCompactionCompletedEvent` 加字段”的二选一。`ContextCompactionCompletedEvent` 继续表示 summary compaction 成功；candidate proposal 是附加事实，使用独立 typed event。

### 6.2 必须接入 event union / publish filters

新增事件后必须同步：

- `EventType`
- `AgentEvent` union / event log contract
- `HostSession._compaction_events_after()` publish filter
- mid-turn inline compaction 的 compaction event publish filter
- CLI compaction notice 若需要展示候选信息
- inspector projection
- serialization/replay tests

否则 event log 里会有事实，但 live runtime publisher / CLI / inspect 看不到。

### 6.3 Inspector projection

Inspect 应能回答：

```text
这次 compaction 产生了哪些 memory candidates？
哪些 pending？
哪些后来被 governance submit/skip/merge？
哪些最终进入 graph？
```

现有 inspector 已能通过 `memory_candidates` / `memory_governance_decisions` / `memory_write_outbox` 查不少内容，但需要把 `origin=compaction` 和 `metadata.compaction_id` 投影出来。

## 7. Governance Prompt 需要知道 compaction 候选较弱

当前 governance prompt 已经有：

```text
- Prefer skip over weak memory.
- Do not submit durable memory for one-off task details.
- Use ctx:user only for durable user-wide preferences or habits.
- Use exact workspace scope for current-project facts or decisions.
```

建议补充：

```text
Compaction-origin candidates are model-inferred observations from context compression.
Treat them as weaker than explicit user memory-tool proposals.
Submit only if the candidate is durable, repeated, project-relevant, and useful for future runs.
Skip if it is a one-off task detail, a transient implementation step, a secret, or an overgeneralized habit.
Do not upgrade compaction-origin candidates to explicit_user_instruction unless the source events clearly contain a direct user instruction to remember it.
```

同时 governance snapshot 需要包含 candidate metadata，尤其：

```json
{
  "origin": "compaction",
  "metadata": {
    "compaction_id": "...",
    "included_run_ids": ["..."],
    "through_sequence": 123,
    "summary_artifact_id": "..."
  }
}
```

当前 `MemoryGovernanceEngine._candidate_snapshot()` 已经从 `candidate.model_dump(mode="json")` 开始构造 snapshot。因此实现上只要 `PooledMemoryCandidate` 模型/schema 增加 `metadata`、`source_artifact_id`、`source_event_id`、`intent_fingerprint`，治理输入会天然带上这些字段。

但 compaction candidate 的真实依据可能横跨多个 runs/window。当前 `_candidate_snapshot()` 只按 `candidate.source_run_id` 拉 source events；对 `origin=compaction` 来说这不够。PR4 必须增加 compaction-aware evidence view：

```text
if candidate.origin == "compaction":
  use bounded metadata.included_run_ids / through_sequence / keep_after_sequence
  include bounded source event summaries across the compaction window
  include summary_artifact_id and metadata.summary_excerpt if available
  mark source_run_id/source_turn_id as attribution_context, not evidence_window
else:
  keep existing source_run_id event snapshot behavior
```

V1 的 clipped summary excerpt 来源是 candidate metadata，不是 archive lookup。PR3 append candidate 时必须把 `summary_excerpt`、`summary_excerpt_chars`、`summary_excerpt_truncated` 写入 metadata；PR4 governance 只读取这份 bounded excerpt。这样 governance 层不需要新增 archive reader 依赖，执行侧也不会为了治理证据同步读取巨大 artifact。

这个 evidence view 必须 bounded，避免把整段 compaction window 又塞回 governance prompt。建议使用：

```text
max_included_runs_for_governance = 5
max_source_events_for_governance = 80
max_summary_excerpt_chars = 2_000
```

同时要同步治理执行侧的 replacement evidence 边界。当前 executor 的 replacement evidence allowlist 主要来自 candidate 原始 `evidence_ids`、`pooled.source_run_id` 的 events 和 `candidate_user_quote`。compaction-aware governance prompt 如果看到跨 run/window evidence，却输出需要 replacement refs 的 `supersede` / `contradict` 决策，apply 阶段可能会拒绝。

V1 冻结为更小的语义：

```text
origin=compaction 的 governance prompt 应优先 submit_as_is / skip / merge / correct。
V1 不支持 compaction-origin candidate 生成需要 replacement evidence refs 的 supersede / contradict 决策。
如果 governance LLM 仍输出这类决策，executor 必须 fail closed / skip 该 decision，并记录 diagnostic，例如 compaction_origin_replacement_evidence_unsupported。
```

未来如果要允许 compaction-origin supersede / contradict，必须同时扩展 executor 的 replacement evidence allowlist，使它使用同一个 bounded compaction evidence view，而不是只改 prompt。

## 8. 安全策略

### 8.1 不记录 secret

compaction extraction prompt 必须明确禁止：

- API keys / tokens / passwords
- private auth headers
- `.env` 内容
- credential-bearing URLs
- user personal sensitive data unless explicitly asked to remember

parser 层也应做粗过滤：

```text
sk-...
Bearer ...
api_key
password
token
secret
authorization
```

命中后 candidate 不进入 pool，只写 diagnostic。

diagnostic / skipped payload 也必须脱敏。命中 secret-like 文本时，不得把原始 `statement`、`reason`、raw candidate JSON 原样写入 event metadata、candidate metadata、logs 或 inspector；只能写：

```json
{
  "code": "compaction_candidate_secret_like_content",
  "redacted": true,
  "field": "statement"
}
```

### 8.2 不把 release/push 习惯升级成自动动作

类似：

> 用户经常让我 main -> release -> push

只能是 `Preference`，不能是 `ActionBoundary`。

也就是说它可以帮助 agent 下次理解“照之前方式同步 release”，但不能让 agent 在没有明确请求时自动 push。

### 8.3 不对 transient workspace 写入 project habit

transient workspace 没有稳定项目身份，compaction candidate 默认 disabled。

## 9. PR 计划

### PR1：schema + candidate origin

改动：

- `CandidateOrigin.COMPACTION`
- `PooledMemoryCandidate.source_event_id`
- `PooledMemoryCandidate.source_artifact_id`
- `PooledMemoryCandidate.intent_fingerprint`
- `PooledMemoryCandidate.metadata`
- Postgres `memory_candidates` schema `ALTER TABLE`
- `CandidatePoolProposal.to_pooled()` 字段传递
- `PostgresCandidatePool.append_candidate()` insert 字段
- `_candidate_from_row()` select/row parse 字段
- `MemoryWriteUnitOfWork` / `CandidateDecisionRepository.append_candidate()` insert 字段
- `InMemoryCandidatePool` round-trip
- candidate row round-trip tests

验收：

- `origin=compaction` candidate 能 append/list/get。
- `metadata` round-trip。
- `intent_fingerprint` round-trip。
- pending 列表包含 compaction candidate。
- append-level duplicate pending compaction candidate 能被识别并跳过或诊断。
- governance-origin candidate 仍不进入 pending。

### PR2：compaction output parser

新增：

- `ContextCompactionMemoryCandidatePolicy`
- `CompactionMemoryCandidateOutput`
- `CompactionCandidateParseResult`
- `CompactionCandidateDiagnostic`
- `CompactionCandidateSkippedItem`
- `parse_compaction_memory_candidates(raw_text)`
- candidate validation / filtering / secret redaction

规则：

- 只接受 `Preference`
- source authority/status 强制为 `conversation_evidence` / `inferred`
- evidence_ids 强制为空
- project workspace 下 scope 由 runtime 强制为当前 workspace scope，不接受 LLM 的 `ctx:user`
- transient workspace 下 V1 禁用 compaction candidates
- candidate parse failure 只在 summary 已成功解析后降级为 diagnostic；summary malformed 仍使 compaction 失败
- extraction trigger / max candidates / mid-turn behavior 由 `ContextCompactionMemoryCandidatePolicy` 决定

验收：

- `<summary>` 正常提取。
- `<memory_candidates_json>` 正常提取。
- malformed candidate JSON 不影响已经成功解析的 summary。
- malformed summary 仍 fail-closed。
- secret-like candidate 被 skip，且 skipped/diagnostic 不泄漏原文。
- LLM 输出 `ctx:user` 时 runtime 强制 workspace scope 或 skip。

### PR3：ContextCompactionService append candidates

改动：

- 增加 optional candidate sink。
- `compact()` 成功写 summary artifact 并 append stored `ContextCompactionCompletedEvent` 后，best-effort append candidate pool。
- append candidate 时把 bounded `summary_excerpt` 写入 metadata，避免 governance 层新增 archive reader。
- 如果有成功 entry，candidate pool commit 后 append `ContextCompactionMemoryCandidatesProposedEvent`。
- 如果没有成功 entry，但 extraction 产生 skipped / duplicate / diagnostics，应 append `proposed_count=0` 的 `ContextCompactionMemoryCandidatesProposedEvent` 作为 audit。
- `ContextCompactionMemoryCandidatesProposedEvent` 是唯一 candidate proposal audit event，不在 completed event 上加候选字段。
- manual compaction 可由 HostSession best-effort trigger-aware schedule governance；preflight auto compaction 延迟到 run finish / safe point；sink 不持有裸 governance notifier，service 不主动 notify governance，也不直接依赖尚未构造好的 `MemoryGovernanceEngine`。
- V1 对 `metadata.phase="mid_turn"` 的 inline compaction 默认不 notify governance；可以直接禁用 candidate extraction。

验收：

- manual / eligible auto compaction 可产生 pending candidate；mid-turn V1 默认不提取。
- candidate append 失败不使 compaction summary 失败。
- proposed event 写失败不使 compaction summary 失败。
- proposed event 只引用已成功进入 pool 的 candidate entry ids。
- malformed candidate JSON / all-skipped / all-duplicate 时可以写 zero-proposal audit event，diagnostics 不丢。
- event 写失败后 inspector 仍能通过 `memory_candidates.metadata.compaction_id` 反查 orphan candidates。
- publish filters 会发布 `ContextCompactionMemoryCandidatesProposedEvent`，live path 不丢事件。
- mid-turn compaction 不同步运行 governance。
- preflight compaction-origin candidate 不影响触发该 compaction 的同一 user turn recall/projection。
- pending candidate 不参与 recall；accepted memory 也必须受 generation/cutoff barrier 约束，不能回流到同一 triggering turn。

### PR4：governance prompt / snapshot 支持 compaction provenance

改动：

- governance prompt 增加 compaction-origin 弱证据规则。
- governance snapshot 对 `origin=compaction` 使用 bounded compaction evidence view，而不是只看 `source_run_id`。
- executor 对 compaction-origin replacement evidence 决策 fail closed，除非未来实现同一 bounded evidence allowlist。

验收：

- governance 能 skip weak compaction candidate。
- governance 能 submit durable project preference candidate。
- governance input 中包含 `origin=compaction`、`metadata`、`source_artifact_id`、`source_event_id`、`intent_fingerprint`。
- governance input 中 `origin=compaction` 的 evidence view 包含 bounded included runs / metadata.summary_excerpt / attribution-vs-evidence distinction。
- governance 对 compaction-origin supersede/contradict replacement refs 不会绕过 executor allowlist；V1 应 fail closed / skip 并记录 diagnostic。
- duplicate canonical memory 仍由 governance dedupe/skip。

### PR5：inspect / real dogfood

改动：

- inspector 展示 compaction candidates。
- dogfood 测试：制造多轮 “main -> release -> push” 习惯，触发 auto-compaction，确认 candidate 进入 pool；governance 接受后进入 workspace memory。

验收：

- inspect 能从 compaction event 找到 candidate pool entries。
- candidate governance decision 可追踪到 canonical memory。
- accepted memory 进入 `memory_nodes` / search index / Oxigraph outbox。

## 10. 测试矩阵

### Unit

- `test_candidate_origin_compaction_round_trips`
- `test_compaction_candidate_pool_metadata_round_trips_postgres`
- `test_compaction_candidate_pool_intent_fingerprint_round_trips`
- `test_memory_write_unit_of_work_preserves_compaction_candidate_metadata`
- `test_parse_compaction_summary_and_memory_candidates`
- `test_parse_compaction_candidate_failure_does_not_drop_summary`
- `test_parse_compaction_malformed_summary_still_fails_closed`
- `test_compaction_candidate_secret_filter_skips_candidate`
- `test_compaction_candidate_secret_filter_redacts_skipped_diagnostics`
- `test_compaction_candidate_forces_conversation_evidence_inferred`
- `test_compaction_candidate_runtime_forces_workspace_scope`
- `test_context_compaction_memory_candidate_policy_defaults`
- `test_context_compaction_missing_candidates_block_policy_defaults_to_ignore`
- `test_parse_compaction_candidate_missing_block_can_be_diagnostic`
- `test_strip_compaction_analysis_rejects_tagless_summary_with_memory_candidates`
- `test_compaction_prompt_can_omit_memory_candidate_instructions`
- `test_compaction_candidate_event_ids_stay_in_metadata_not_evidence_ids`
- `test_context_compaction_memory_candidates_proposed_event_serializes`

### Integration

- `test_context_compaction_appends_pending_memory_candidate`
- `test_context_compaction_proposed_event_only_after_pool_commit`
- `test_context_compaction_candidate_event_write_failure_leaves_orphan_candidate_inspectable`
- `test_context_compaction_candidate_pool_failure_does_not_fail_summary`
- `test_context_compaction_partial_candidate_append_failure_keeps_successful_entries`
- `test_context_compaction_memory_candidate_policy_disabled_omits_prompt_and_audit`
- `test_context_compaction_transient_workspace_does_not_write_candidate_audit_event`
- `test_context_compaction_candidate_publish_filter_includes_proposed_event`
- `test_context_compaction_zero_proposal_audit_event_for_all_skipped_or_duplicate`
- `test_context_compaction_candidate_metadata_contains_bounded_summary_excerpt`
- `test_preflight_compaction_candidate_does_not_affect_same_turn_recall`
- `test_mid_turn_compaction_does_not_run_governance_for_candidates`
- `test_duplicate_pending_compaction_candidate_is_not_appended_twice`
- `test_context_compaction_candidate_does_not_enter_recall_before_governance`
- `test_governance_compaction_candidate_uses_bounded_window_evidence_view`
- `test_governance_compaction_candidate_uses_metadata_summary_excerpt_without_archive_reader`
- `test_governance_compaction_origin_replacement_evidence_decision_fails_closed`
- `test_governance_skips_weak_compaction_candidate`
- `test_governance_accepts_workspace_preference_from_compaction_candidate`
- `test_inspector_links_compaction_to_candidate_to_governance_decision`

### Real / dogfood

- `test_real_llm_compaction_extracts_project_workflow_memory_candidate`
- `test_real_llm_governance_accepts_repeated_project_workflow_preference`

## 11. 对当前图数据库 schema 的判断

正式 canonical graph schema 不需要为了 V1 改。

原因：

- pending candidate 仍存在 `memory_candidates`。
- accepted memory 仍通过 `MemoryWriteService` 进入 `graph_documents` / `memory_nodes`。
- lifecycle / contradiction / supersede 仍走现有 `memory_relations`。
- Oxigraph 仍通过 `memory_write_outbox` 追 canonical writes。

需要改的是 candidate pool schema，不是 graph schema。

唯一要小心的是 evidence：

```text
candidate.evidence_ids 是 graph Evidence node id，不是 event id。
```

所以 V1 不要把 compaction event id 写进 evidence_ids。否则 governance submit 后 ledger 会找不到 graph node，导致 write failed。

## 12. 推荐的 V1 行为

V1 可以先实现非常窄的一条路径：

```text
manual / eligible auto compaction
  -> parse Preference-only memory_candidates_json
  -> force project scope when project workspace exists
  -> force conversation_evidence + inferred
  -> evidence_ids=[]
  -> compute intent_fingerprint
  -> store bounded summary_excerpt in candidate metadata
  -> append CandidateOrigin.COMPACTION candidate if any valid non-duplicate candidate exists
  -> append ContextCompactionMemoryCandidatesProposedEvent when extraction was attempted, including proposed_count=0 audit cases
  -> manual compaction may let HostSession best-effort schedule governance through trigger-aware scheduler
  -> preflight auto compaction delays governance visibility until the triggering run finishes or a recall/projection cutoff barrier guarantees no same-turn contamination
  -> mid-turn inline compaction disables extraction by default in V1
```

不做：

- 直接写 graph
- 直接写 Evidence nodes
- Claim/Decision/Observation/ActionBoundary extraction
- user confirmation UI
- candidate 直接进入 recall

这条路径已经足够支持“Pulsara 汲取本项目开发习惯”，同时仍保持治理边界清楚。
