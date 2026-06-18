# Pulsara 记忆 Scope/Domain v1 实施设计

_Created: 2026-06-17_

> 本文是「跨对话记忆互通，但不全量」的**可落地实施设计**：钉死宿主契约、scope 词汇、两层写闸、共享 graph 策略、读侧 resolver、working context 入库不入图，以及不变量与测试。
>
> **核心原则**：**scope 先可信，graph 再共享；working context 入库但不入图。**
>
> 所有「代码现状」主张均经 `file:line` 核实（§0）。代码签名/契约是规范性的；散文是解释性的。本文已把对原计划的修订直接并入（§11 汇总改了什么、为什么）。

## 0. 已核实代码基线（实现起点，勿再勘探）

- `LoopState.current_scope: str | None = None`（[state.py:65](src/pulsara_agent/runtime/state.py:65)）；agent.py 多处把 `state.current_scope or "session"` 写进 projection event metadata（[agent.py:399](src/pulsara_agent/runtime/agent.py:399)）。但 normal recall 真正传给查询的是 `durable_hooks.project()` 的 `scopes=(state.current_scope,) if state.current_scope else ()`（[hooks/durable.py:61](src/pulsara_agent/memory/hooks/durable.py:61)）——当 `current_scope is None` 时是**空集合 = 无 scope 过滤**，不是 `"session"` 过滤。resolver 是净新增。
- `MemoryGovernanceExecutor` 字段仅 `graph / runtime_session_id / graph_id / memory_write_uow_factory`（[executor.py](src/pulsara_agent/memory/governance/executor.py)）。**无 domain context / allowed_write_scopes** → 写集闸需新字段穿入。
- 全仓**无** `DomainContext / memory_domain / allowed_write_scopes / ScopeResolver / read_scopes`（grep 零命中）——全部净新增。
- `write_gate.evaluate_*` 只校验 `scope.strip()` 非空（[write_gate.py:39](src/pulsara_agent/memory/canonical/write_gate.py:39) 等），**不校验 scope 值** → 当前 `ctx:乱填` 也能过。
- 召回 scope 过滤是**精确集合成员**：`view.scope not in query.scopes`（[recall/service.py:215](src/pulsara_agent/memory/recall/service.py:215)），非层级匹配。
- `durable_hooks.project()` 目前只传 `scopes=(state.current_scope,) if state.current_scope else ()`（[hooks/durable.py:61](src/pulsara_agent/memory/hooks/durable.py:61)）——单点，且缺省为空集合，需改 resolver 集合。
- 显式 `memory_search` 工具同样存在缺省无 scope 过滤：不传 `scope` 时 `RecallQuery.scopes=()`（[memory_query.py:93](src/pulsara_agent/tools/builtins/memory_query.py:93)）。共享 graph 后它也是跨 workspace 泄露通道，必须和 normal projection recall 一起接 read-scope resolver。
- `candidate.scope: str` 在 `MemoryCandidateBase` 上可读（[event/candidates.py:26](src/pulsara_agent/event/candidates.py:26)）——executor 写集闸能读到。
- 默认 `resolved_graph_id = graph_id or "graph:runtime/<runtime_session_id>"`（[wiring.py:124](src/pulsara_agent/runtime/wiring.py:124)）——**默认 per-session 分区**，是对话互不相通的根因；`graph_id` 全程可注入。
- governance engine / `submit_pending_as_is` 的 orchestrated path 会过滤本 runtime 的候选：`candidate.source_session_id == self.runtime_session_id`（[governance/engine.py:85](src/pulsara_agent/memory/governance/engine.py:85)）。但 `MemoryGovernanceExecutor.apply_decision()` 本身只校验 target entry 存在，**还没有硬性拒绝跨 runtime target**。§4.3 必须把这个 target ownership gate 补进 executor，而不能只依赖调用路径约定。

## 1. 目标与不变量

让 codex-like 的桌面应用（即 Pulsara 自身——让 Pulsara 像 codex 那样工作）的不同对话**互通记忆，但不全量**：

- `ctx:user`：跨所有对话可见（用户偏好、长期工作习惯，如「用根目录 .venv 的 uv，不用系统 python」）。
- `ctx:workspace/<key>`：仅同一真实项目内可见。
- 临时 codex-like 对话目录**不生成 workspace scope**。
- v1 **不做** `ctx:conversation`。
- canonical memory 进图；working context 只进 Postgres operational 表，**不进图**。

**全局不变量（验收任何 PR 的硬约束）：**

- **SD-1**：normal recall 的可见性 = `graph_id`（物理分区）∩ `read_scopes`（语义过滤）。单用户下 graph 不再当隔离墙，隔离靠 scope。
- **SD-2**：候选只能写进**本 runtime 的 `allowed_write_scopes`**；越界 → no-write/skip/NEEDS_REVIEW，**绝不自动改写 scope**。
- **SD-3**：scope 值必须属于受控词汇（§2）；自由文本/未知前缀/层级歧义在 write_gate 被拒。
- **SD-4**：`ctx:user` 跨所有对话（transient/project）可见可写。
- **SD-5**：working context 永不进 `memory_nodes/memory_relations/memory_search_index`、不过 governance/supersede；它是 sink + read-for-injection，**绝不作 candidate 生成的 source**。
- **SD-6**：后端**不**从 cwd 路径推断「真项目 vs 临时目录」；该判定由宿主显式传入（§3）。

## 2. Scope 词汇（v1 扁平精确，非层级）

```text
ctx:user                      # 用户级，跨所有对话
ctx:workspace/<hash_id>       # 项目级，仅 project 模式存在；由后端从 stable_project_key 派生
```

规则：

- **扁平精确匹配**：召回 `view.scope in query.scopes` 是精确相等（[recall/service.py:215](src/pulsara_agent/memory/recall/service.py:215) 现状）。v1 **不做前缀/层级匹配**。
- **`<hash_id>` 必须是扁平 id，不含 `/`**：否则 `ctx:workspace/a/b` 看起来像层级、破坏精确匹配的扁平假设。`<hash_id>` **不由宿主拼接**，由后端从 `stable_project_key` canonicalize 后 `sha256(... )[:16]` 派生。可读名另存 `workspace_label`。
- **字符集硬约束**：`memory_domain_id` 和 workspace scope 的 `<hash_id>` 都使用 flat id 规则：ASCII 小写字母/数字开头，后续仅允许小写字母、数字、`.`、`_`、`-`，长度 1-128。`stable_project_key` 可以是宿主提供的真实项目绝对路径；它不会原样进入 scope/graph id。
- **语法校验集中一处**：新增 `memory/scope.py`，提供 `is_valid_scope(s) -> bool` 与 `parse_scope`，**读写两侧共用**，避免词汇漂移。

```python
# memory/scope.py（净新增，读写共用）
import re

CTX_USER = "ctx:user"
_WORKSPACE_PREFIX = "ctx:workspace/"
_FLAT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

def is_valid_flat_id(value: str) -> bool:
    return bool(_FLAT_ID_RE.fullmatch(value))

def is_valid_scope(scope: str) -> bool:
    if scope == CTX_USER:
        return True
    if scope.startswith(_WORKSPACE_PREFIX):
        key = scope[len(_WORKSPACE_PREFIX):]
        return is_valid_flat_id(key)
    return False
```

## 3. Phase 0：宿主契约 `MemoryDomainContext`

后端不猜，宿主传。【新增】一个值对象，wiring 期注入：

```python
@dataclass(frozen=True, slots=True)
class MemoryDomainContext:
    memory_domain_id: str               # 稳定用户级 flat id（local-user hash/slug）
    workspace_kind: Literal["project", "transient"]
    stable_project_key: str | None      # project 才有；通常是真实项目绝对路径；transient = None
    workspace_label: str | None = None  # 可读名，如 "tau3-agenticrl"，仅展示用

    @property
    def graph_id(self) -> str:
        return f"graph:user/{self.memory_domain_id}"   # 单用户共享分区（§5）
```

**契约要点（守 SD-6）：**

- 「这个 cwd 是真项目还是 codex-like 临时目录」**只有宿主知道**——后端看到的两者都只是路径，无法分辨。宿主必须给 `workspace_kind` + `stable_project_key`。
- `transient` → `stable_project_key = None` → 不产 workspace scope。
- `memory_domain_id` 谁来算也是宿主职责（如机器本地用户 hash）。
- `memory_domain_id` 仍必须通过 `is_valid_flat_id`，因为它会进入 `graph:user/<id>`。
- `stable_project_key` 只要求非空；后端负责 `expanduser`/`resolve(strict=False)` 规范化路径样式 key，并从 canonical key 派生 workspace hash scope。宿主不拼 `ctx:workspace/...`，避免 scope 协议散落到 UI/入口层。

## 4. Phase 1：写侧 scope 纪律（前置，必须早于共享 graph）

这是整套方案的承重前置。**§0 已证实 scope 今天零值校验、自由文本**——若先打开共享 graph 再治理写，等于先拆墙再装锁（SD-1/SD-2 同时失效）。所以本 Phase 必须**先于或同 PR 于** Phase 3。

每个 runtime 由 domain context 算出可写集：

```text
allowed_write_scopes(domain):
  transient: {ctx:user}
  project:   {ctx:user, workspace_scope(stable_project_key)}
```

两层闸，职责严格分离:

### 4.1 write_gate —— 语法闸（无 runtime 上下文，只判「合不合法」）

`write_gate` 在 ledger 内、没有 domain context,所以它**只能做语法校验**,不能做成员校验:

- 把现有 `if not scope.strip()` 升级为 `if not is_valid_scope(scope)`（§2 共用函数）。
- 拒绝:空串、`ctx:乱填`、未知前缀、`ctx:workspace/a/b`（层级歧义）。
- 通过:`ctx:user`、`ctx:workspace/<hash_id>`。
- 失败 → 维持现有 `WriteDecision(False, REJECTED, ...)` 语义。

### 4.2 governance executor —— 成员闸（有 runtime 上下文，判「该不该写到这个 scope」）

【改造】`MemoryGovernanceExecutor` 加字段 `allowed_write_scopes: frozenset[str]`（由 wiring 从 domain context 算好注入,§0 已确认 executor 当前无此字段）。在 `_candidate_for_decision` 取出 candidate 后、submit 前:

```python
if candidate.scope not in self.allowed_write_scopes:
    # SD-2：越界不自动改 scope，降级为 skip / NEEDS_REVIEW
    return _skip_out_of_scope(decision, candidate.scope)   # no-write
```

- transient runtime 提交 `ctx:workspace/<key>` → 不在 `{ctx:user}` → skip。
- project runtime 提交 `ctx:user` 或本项目 scope → 通过。
- **绝不自动改写 scope**（SD-2）:改写 = 替 LLM 猜意图,正是要避免的。越界就是越界,记 skip。

> 分工总结:**write_gate 答「这个 scope 合法吗」(语法、无上下文);executor 答「这个 runtime 能写这个 scope 吗」(成员、有上下文)。** 二者缺一不可——只有语法闸挡不住 transient 写 project；只有成员闸挡不住 `ctx:乱填`。

### 4.3 关键细节:governance 是 detached 的,成员闸怎么对齐到「发起 runtime」?

这是原计划没点破、但会真出 bug 的地方。reflector / governance 产出的候选**不是当场写**,而是进候选池、由 governance 批次异步处理。那么成员闸该按**谁的** allowed_write_scopes 校验?

当前 orchestrated path 已经按本 runtime 过滤候选：governance engine / `submit_pending_as_is` 都只处理 `source_session_id == self.runtime_session_id`。但这还不是 executor 层不变量，因为 `apply_decision()` 可以被直接调用，今天只验证 target entry 存在。因此 PR2 必须把「target ownership」提升为 executor 硬闸:

```python
def _validate_target_entries(self, decision: GovernanceDecision) -> tuple[PooledMemoryCandidate, ...]:
    targets = tuple(self.candidate_pool.get_candidate(entry_id) for entry_id in _target_entry_ids(decision))
    wrong_session = [target.entry_id for target in targets if target.source_session_id != self.runtime_session_id]
    if wrong_session:
        raise ValueError(f"governance decision targets candidates from another runtime: {wrong_session}")
    return targets
```

有了这道闸之后:

- executor 的 `allowed_write_scopes` = **它自己这个 runtime 的**;
- 它只能治理**本 runtime 发起**的候选;
- 因此「按发起 runtime 校验」才真正成立,无需把 allowed scopes 持久化到候选上。

> v2 警示:若将来 governance 变成**跨 runtime**批处理(治理别的 session 的候选),这个自动对齐就破了——那时必须把发起时的 `allowed_write_scopes`(或 domain context)**持久化到候选**,在治理时按候选自带的约束校验,而不是按当前 executor 的。**v1 不跨 runtime,不做此持久化;但必须写进代码注释,免得 v2 默默踩雷。**

## 5. Phase 2：写入提示与治理输入对齐

让主 agent、reflector、governance 都知道**当前 runtime 允许的 scope**——但这是「第一道软约束」，硬保证仍靠 §4 的闸。

- `remember_*` 工具描述 / 主 agent 上下文：注入当前可写 scope 清单（动态，按 domain）：
  ```text
  Allowed scopes in this run:
  - ctx:user
  - ctx:workspace/<key>     # 仅当 workspace_kind == project
  ```
- 赋值规则（写进 prompt + reflector/governance few-shot）：
  - 用户长期偏好、通用工作方式 → `ctx:user`
  - 项目技术决策、路径、依赖、架构约束 → `ctx:workspace/<key>`
  - 当前一次性任务细节 → **不提 durable memory candidate**（既不 user 也不 workspace）
- reflector/governance few-shot 已在用 `ctx:user`/`ctx:workspace`（[governance/engine.py:259](src/pulsara_agent/memory/governance/engine.py:259)、[reflection/engine.py:499](src/pulsara_agent/memory/reflection/engine.py:499)）——把它们从「示例」升级为「按当前 domain 注入的可写集」，并去掉「scope 是自由文本」的旧描述。
- 实现注意：`remember_*` 工具今天的 `parameters` 是 class-level 静态字段（[memory.py](src/pulsara_agent/tools/builtins/memory.py)），不能直接把某个 runtime 的 allowed scopes 写进全局 class var。两条可行路线二选一：
  - 把动态 allowed scopes 放进 runtime/system prompt，不改工具 schema；
  - 或把 `parameters` 改为实例级 property / factory，让 registry 按 domain 构造工具实例。
  v1 推荐先用 runtime/system prompt 注入，避免全局 schema 被某个 runtime 污染。
- 现有 fixture / prompt / real-LLM smoke 里还有 `ctx:project`、裸 `ctx:workspace` 或语义化 workspace scope（如 durable memory tests 与 reflection Example F）。PR1/PR3 必须迁移为 `ctx:user` 或后端派生的 `ctx:workspace/<id>`，否则新语法闸/成员闸会把旧测试当非法或越界 scope 拒掉。

> 为什么 prompt 不够、必须有 §4 硬闸：prompt 是概率性的，LLM 仍可能把任务细节误标 `ctx:workspace`。§4.2 成员闸是确定性兜底——LLM 提议，gate 裁定。和 supersede 同构。

## 6. Phase 3：共享 Graph 策略 + 读侧 `ScopeResolver`（同 PR 落地）

写侧可信后，再打开跨对话物理共享。但**共享 graph 与读侧 resolver 必须同 PR 落地**：如果只把 `graph_id` 改成 `graph:user/<id>`，而 normal recall 仍在 `current_scope is None` 时传 `scopes=()`，就会在共享 graph 上无 scope 过滤，workspace A 可召回 workspace B。这个顺序风险已经由 §0 的代码事实证实。

```text
graph_id = graph:user/<memory_domain_id>     # 来自 MemoryDomainContext.graph_id
```

- 普通 codex-like 对话与 workspace 对话**都落在同一个用户 memory domain**。
- 「互通但不全量」**不靠 graph 隔离，靠本 Phase 的 scope 精确过滤**（SD-1）。
- wiring 改造：`build_durable_runtime_wiring` 的 `resolved_graph_id` 从 `graph:runtime/<session>`（[wiring.py:124](src/pulsara_agent/runtime/wiring.py:124)）改为「优先用 domain context 的 `graph:user/<id>`，未提供 domain 时回退旧的 per-session（保持测试/无宿主场景隔离）」。

同一 PR 中新增 `ScopeResolver`，按 domain 算出**可达读集**：

```text
read_scopes(domain):
  transient: {ctx:user}
  project:   {ctx:user, workspace_scope(stable_project_key)}
```

落地:

- 【新增】`ScopeResolver`（输入 `MemoryDomainContext` → 输出 `read_scopes: frozenset[str]`）。v1 它和 `allowed_write_scopes` 形状相同——**但保持两个独立函数**（读/写未来会分叉:如 v2 可能「能读 workspace、只能写 user」）。
- 【改造】`durable_hooks.project()` 把 `scopes=` 从单点换成 `resolver.read_scopes(domain)`。
- 召回过滤逻辑**不动**:`view.scope in query.scopes`（精确匹配，service.py:215）天然支持集合。
- v1 **不做**层级匹配、**不做** `ctx:conversation`。

这样:transient 对话召回到 `ctx:user`(全局偏好流过来,SD-4)但召回不到任何 workspace 记忆;project 对话额外召回到本项目 scope,但**召回不到别的 workspace**(SD-1)。

> 截图里「对话模式的对话互通」**主要由这条 + 共享 graph 实现**:它们共享 `graph:user/<id>`,且都读 `ctx:user` → 用户偏好在所有对话间流动;而各自的 workspace 记忆(若有)互不可见。

**迁移决定（显式，非静默）：**

- 旧的 `graph:runtime/<session>` 分区记忆**不会自动可见**（新默认是 `graph:user/<id>`）。
- 鉴于 recall/supersede 仍早期、生产 canonical 记忆极少，**可接受搁浅**；后续如需要再做迁移工具。这条必须写进 PR 描述，让评审知情。

## 7. Phase 4：显式 memory_search 也必须 scope-aware

共享 graph 后，显式 `memory_search` 不能继续把「未传 scope」解释为「查整个 graph」。它和 normal projection recall 一样，默认必须受 `read_scopes` 约束:

- 【改造】`MemorySearchTool` 接收 `read_scopes: frozenset[str] | None` 或 `ScopeResolver`。
- 用户未传 `scope` 时，`RecallQuery.scopes=tuple(read_scopes)`，而不是 `()`。
- 用户显式传 `scope` 时，先校验 `is_valid_scope(scope)`，再校验 `scope in read_scopes`；越界返回 tool error/empty + guidance，**不执行跨 scope 查询**。
- `MemoryGetTool` / `MemoryRelatedTool` / `MemoryExplainTool` 按 id 读取 canonical node，也需要在返回前检查 fetched node 的 `scope in read_scopes`；否则共享 graph 下用户知道 memory id 就可跨 workspace 读。若 node scope 越界，返回 not_found/forbidden-style payload，避免泄露内容。
- tool schema 的 scope 描述也要从 `ctx:workspace/project` 更新为“当前可见的精确 `ctx:workspace/<id>`”，并提示“默认只搜索当前可见 scopes”。

这一步可以和 Phase 3 同 PR 做；若拆 PR，必须保持 graph 仍是 per-session fallback，不允许出现「共享 graph + memory_search 无 read-scope」的中间状态。

## 8. Phase 5：Working Context 入库不入图（domain/scope 之上）

domain/scope 稳定后再做。它用**同一套 domain/scope**,但**绝不进**:`memory_nodes` / `memory_relations` / `memory_search_index` / 5 类 typed node / governance / supersede（SD-5）。

【新增】operational 表（与 EventLog/Artifact 平级,不入 `memory_schema.py` 的 canonical substrate;放 operational schema）:

```sql
CREATE TABLE IF NOT EXISTS working_context_summaries (
    summary_id        TEXT PRIMARY KEY,
    memory_domain_id  TEXT NOT NULL,
    summary           TEXT NOT NULL,        -- 小：一两句「用户最近在做什么」
    workspace_label   TEXT,                 -- 可选元数据：这段活动发生在哪个 workspace（支撑延伸价值，非过滤键）
    workspace_key     TEXT,                 -- 可选元数据；transient 为 NULL
    source_session_id TEXT NOT NULL,
    source_run_id     TEXT NOT NULL,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at        TIMESTAMPTZ,
    metadata          JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (memory_domain_id)               -- domain 级 last-write-wins：一个 domain 一条「最新近期活动」
);
CREATE INDEX IF NOT EXISTS idx_working_context_domain
    ON working_context_summaries(memory_domain_id, updated_at);
```

写/读:

- **写**:run 末(`on_session_end` 实际是 per-run 粒度,见下注)按 `memory_domain_id` 覆盖(last-write-wins)。不进 UoW、不过 governance。**所有对话都有资格写**(含 transient),但 summarizer 必须允许 `should_update=false`：低信号/no-op/寒暄/空转 run 不覆盖旧摘要。见下「定位修正」。
- **读**:每次 projection 前,按 `memory_domain_id` 加载**未过期**的最新 summary,**像 `ctx:user` 一样几乎总是注入**,作为**独立 fenced block**,标注「用户近期在做什么·非长期记忆」,带 `do_not_write_back`。**保持小**(一两句,token 预算很低)。

**低信号覆盖闸（deterministic floor，summarizer 不能覆盖）：**

`should_update` 不能只靠 LLM 判断。working-context summarizer 可以建议 `should_update=true/false`，但写入前还要过一层机械 no-op guard；guard 不通过时一律不覆盖旧摘要:

- 新 summary 为空或低于最小长度（例如 `< 24` chars）→ 不覆盖。
- 本 run 无 tool calls、无 durable memory candidates、无文件/终端/检索等实质事件，且用户输入只是短寒暄/低信息量文本 → 不覆盖。
- 新 summary 与现有 summary 规范化后相同或近似相同 → 不覆盖，只可选择刷新 `updated_at`（v1 可先不刷新）。

这和 supersede 的 ACTIVE-gate 同类：LLM 可以提议，deterministic floor 决定是否允许破坏旧状态。v1 推荐先实现这层 cheap guard；不要把「低信号不覆盖」完全交给 prompt。

**定位修正(本轮纠正,推翻上一版 transient 规则):**

- working context 的**核心价值不是「同一项目接着干」**——那只是它的**延伸价值**。它的核心是:**像 `ctx:user` 一样持续注入,告诉 agent「用户最近在做什么」**。一个小而新鲜的近期活动摘要,跨所有对话可见。
- 因此它是 **domain 级、所有对话都有资格写、last-write-wins**:有实质活动的 run 末覆盖成「最新近期活动」。transient 对话**也可覆盖**——这不是污染,这正是它的用途(最新就该覆盖旧的)。但 `hello`、单轮寒暄、无工作进展、明显低信号 run 应返回 `should_update=false`，保留旧摘要。这条**推翻**了上一版「transient 不写 working context / 仅 project 写」的规则，同时避免 last-write-wins 被噪声 run 破坏。
- `workspace_key` 退化为**可选元数据**(记录这段活动发生在哪个 workspace),**不是过滤条件**。它支撑「延伸价值」:当新对话恰好在同一 project 时,可据此多说一句「上次在该项目推进到…」。但默认注入不依赖它。
- 与 `ctx:user` canonical 的分工:`ctx:user` = **耐久的「你是谁、你的规矩」**(治理后事实/偏好);working context = **易变的「你最近在干嘛」**(可过期摘要)。两者都跨对话注入,但一个进图受治理、一个入库不入图(SD-5),语义层级不同,不可混。

> 注:`on_session_*` 钩子**名为 session、实按 run 触发**（[agent.py:131](src/pulsara_agent/runtime/agent.py:131)/285,§0 邻近事实）。本设计的「run 末覆盖写」依赖这一点;命名误导是既有技术债,建议另行清理(改名 `on_run_*` 或加 docstring),不在本设计范围。

## 9. 测试矩阵（每层确定性测试）

| 测试 | 断言要点 | 守 |
|---|---|---|
| write_gate 拒非法 scope | `ctx:乱填` / 空串 / `ctx:workspace/a/b`（层级歧义）→ REJECTED | SD-3 / §4.1 |
| write_gate 放行合法 scope | `ctx:user` / `ctx:workspace/<flat>` → 通过语法闸 | §2 / §4.1 |
| transient 不能写 workspace scope | transient runtime 提交 `ctx:workspace/<key>` → executor skip，无 canonical 写 | SD-2 / §4.2 |
| project 可写 user + 本项目 scope | project runtime 写 `ctx:user` 与 `ctx:workspace/<key>` → 通过 | §4.2 |
| 越界不自动改 scope | 越界候选记 skip/no-write，**scope 原样保留在 skip 记录**，未被改写 | SD-2 |
| executor 拒跨 runtime target | `apply_decision()` 直接指向别的 `source_session_id` 候选 → hard fail / no-write | §4.3 |
| 成员闸对齐发起 runtime | target ownership gate + allowed_write_scopes → 按发起 runtime 约束 | §4.3 |
| normal recall 只回 read_scopes 内 | projection recall 结果 scope ⊆ resolver 的 read_scopes | SD-1 / §6 |
| memory_search 默认受 read_scopes 限制 | 不传 scope 时只查当前 read_scopes，不查整个 graph | §7 |
| memory_search 显式越界被拒 | 显式传 workspace B scope 时，workspace A runtime 不执行查询 | §7 |
| memory_get/related/explain 拒越界 id | 已知 memory id 但 node.scope 不在 read_scopes → 不泄露内容 | §7 |
| 共享 graph 下 A 不召回 B | 同 `graph:user/<id>`，workspace A 的对话**召回不到** workspace B 的 memory | SD-1 |
| ctx:user 跨模式可召回 | transient 与 project 对话都能召回 `ctx:user` memory | SD-4 |
| working context 不进图 | 写 working context 后 `memory_nodes` 无新增行 | SD-5 |
| working context 所有对话都有资格写 | transient 与 project 的实质活动 run 可覆盖写 domain 级 working context（last-write-wins） | §8 定位修正 |
| working context 低信号不覆盖 | `hello`/寒暄/无进展 run 即使 summarizer 建议更新，也被 deterministic no-op guard 拦下，旧摘要保留 | §8 |
| working context 像 ctx:user 注入 | 任意对话 projection 前都加载并注入未过期 working context（独立 fenced block） | §8 |
| working context 按 domain/TTL 加载 | 只加载同 `memory_domain_id` + 未过期；过期不注入 | §8 |
| **real-LLM【必做】project 正确写项目 memory** | project 场景模型把项目决策写 `ctx:workspace/<key>`，偏好写 `ctx:user` | §5 / 验收 |
| **real-LLM【必做】transient 不乱写 workspace** | transient 场景模型不产 `ctx:workspace` 候选（即便产了也被 §4.2 挡） | §4.2 / 验收 |

> real-LLM 两条必做（非可选）：scope 赋值的「语义那一半」只能靠 LLM（§5），类型/事务对了不证明模型按 domain 赋值正确。这是「不全量」可信度的最低验收。

## 10. 落地顺序（每步 tests 全绿再下一步）

严格遵循「scope 先可信，graph 再共享」：

```text
PR1  scope.py 词汇 + is_valid_scope；write_gate 语法闸（§2 + §4.1）
       退出：非法 scope 被拒；合法通过；既有写入测试仍绿
PR2  MemoryDomainContext + allowed_write_scopes；executor 成员闸（§3 + §4.2 + §4.3）
       退出：transient 不能写 workspace；越界 skip 不改 scope；executor 拒跨 runtime target；成员闸对齐发起 runtime
PR3  prompt/tool scope 描述按 domain 注入（§5）
       退出：可写 scope 清单进 prompt；reflector/governance few-shot 对齐
PR4  graph_id 策略 → graph:user/<id> + ScopeResolver 接 normal recall（§6）；写明迁移决定
       退出：normal recall 只回 read_scopes；共享 graph 下 A 不召回 B；ctx:user 跨模式可召回
PR5  显式 memory tools 接 read_scopes（§7）
       退出：memory_search 默认不查全 graph；显式越界 scope/id 不泄露内容
PR6  working_context_summaries 入库不入图（§8）
       退出：不进 memory_nodes；实质活动 run 可覆盖 domain 级 working context，低信号 run 不覆盖；像 ctx:user 一样按 domain/TTL 加载注入
```

**顺序的承重点**：PR1-2（写纪律）**必须先于** PR4（共享 graph）。PR4 内部也必须把共享 graph 与 ScopeResolver normal recall 同时落地；不得出现「共享 graph 已开、projection recall 仍 `scopes=()`」的中间状态。在 scope 可信前打开共享 graph = 关掉隔离（§4 开头）；在 read resolver 接入前打开共享 graph = 绕过隔离（§6 开头）。这是全文最不可妥协的排序。

## 11. 对原计划的修订汇总（改了什么 / 为什么）

本文在原五-Phase 计划基础上做了五处修订，均已并入上文：

1. **顺序倒置纠正**：原计划把「写纪律」列为第 4 步（收尾），本文提为 **Phase 1 前置**，且 PR 顺序硬性要求写纪律先于共享 graph。理由：§0 实证 scope 当前零校验，先共享后治理 = 先拆墙后装锁。
2. **写闸拆成两层**：原计划「write_gate 校验 + executor 校验」语焉不详。本文明确 **write_gate = 语法闸（无上下文）、executor = 成员闸（有上下文）**，并指出二者缺一不可（§4.1/4.2）。
3. **补 governance-detached 对齐（原计划缺）**：成员闸该按「发起 runtime」校验。本文修正为：当前 orchestrated path 会过滤本 runtime 候选，但 executor 仍必须新增 target ownership gate，硬性拒绝跨 runtime target；之后 v1 才能不持久化 allowed scopes 到候选。v2 若做跨 runtime governance，则需把 allowed scopes 或 domain context 持久化到候选（§4.3）。
4. **补共享 graph 与 read resolver 的原子落地要求**：graph:user 共享不能早于 normal recall ScopeResolver；显式 `memory_search/get/related/explain` 也必须受 read scopes 约束（§6/§7）。否则共享 graph 会被 `scopes=()` 或按 id 读取绕过。
5. **working context 定位修正(本轮纠正)**:working context 的核心**不是「同一项目接着干」**(那是延伸价值),而是**像 `ctx:user` 一样持续注入的「用户最近在做什么」小摘要**。因此它是 **domain 级、所有对话(含 transient)都有资格写、实质活动 last-write-wins**;`workspace_key`/`workspace_label` 退为可选元数据,非过滤键(§8)。**这条推翻了上一版「transient 不写 working context / 仅 project 写」的规则,但保留 `should_update=false` 防止低信号 run 覆盖旧摘要。**
6. **workspace scope id 无 `/` + flat id regex 硬约束 + scope 校验集中 `scope.py`**：守住「扁平精确匹配」假设，读写词汇不漂移（§2）。

未改方向：核心原则、目标、graph:user 共享、ScopeResolver、working 入库不入图——这些原计划都对。

## 12. 一句话收口

> **scope 先可信，graph 再共享，working context 入库不入图。** 互通靠共享的 `graph:user/<domain>` + `ctx:user` 跨对话流动；不全量靠 scope 精确过滤（read 侧 resolver）；而这一切的前提是写侧两层闸（语法 + 成员）先把 scope 从「自由文本」治理成「受控且越界即降级」。「对话互通」有两条互补的载体：耐久面是 `ctx:user` canonical 偏好的共享；易变面是 working context——一个**像 `ctx:user` 一样持续注入、所有对话都有资格更新、可过期的「用户最近在做什么」小摘要**（domain 级、入库不入图，「同一项目接着干」只是它的延伸价值）。任何把「打开共享读」排到「管住写」之前的顺序，都会让「不全量」第一天失效。
