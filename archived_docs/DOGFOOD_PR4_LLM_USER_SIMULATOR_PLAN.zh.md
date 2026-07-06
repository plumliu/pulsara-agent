# PR4 Dogfood: LLM User Simulator Plan

本文定义一个两阶段 dogfood 方案：用另一个 real LLM 作为“用户模拟器”，和 Pulsara real LLM 进行长会话交互，持续压测 `trusted_host + approval_policy=on_request + terminal_access=ask` 这条新常态路径。

最终目标不是一次性手工实验，而是把两组 dogfood 固化为 `tests/` 目录下的 opt-in real LLM 测试资产：

1. **4 轮小测试**：验证 simulator / harness / verifier 架构可用。
2. **0-10 长程剧本**：覆盖 PR4、approval resume、user stop、skills、terminal、transcript note 的组合 seam。

当前测试资产刻意关闭 durable memory / reflection，避免把 PR4 权限 dogfood 和 memory 子系统稳定性绑在同一条长测里。Memory recall / pollution audit 保留为后续 Phase 3。

## 0. 设计原则

### 0.1 LLM user simulator 只模拟用户，不直接控制 runtime

用户模拟器负责：

- 根据剧本发自然语言用户消息。
- 根据 Pulsara 回复和 pending approval 摘要决定下一步用户动作。
- 在指定轮次提出 approve / deny / stop 意图。
- 模拟真实用户的模糊表达、追问、继续、拒绝。

用户模拟器不负责：

- 直接调用 HostCore。
- 直接改 event log。
- 直接执行 approval resolution。
- 判断测试是否通过。
- 自由批准任意工具调用。

### 0.2 Harness 是唯一执行者

dogfood harness 负责：

- 创建 HostCore session。
- 固定运行策略：

```text
permission_profile = trusted_host
approval_policy = on_request
terminal_access = ask
```

- 把 Pulsara 的 final text、pending approval summary、session summary 等用户可见信息提供给 user simulator。
- 解析 user simulator 的结构化 action。
- 对 approval / deny / stop 进行确定性校验后执行。
- 记录 event log、pending approval、tool result、skill catalog、session summary；若未来打开 memory，再记录 memory summary。

### 0.3 Deterministic verifier 决定通过/失败

LLM 不能作为最终判定器。Verifier 直接检查结构化状态：

- event types。
- RunEnd status / stop_reason。
- pending approval presence / absence。
- tool call snapshots。
- tool result payloads。
- filesystem artifacts。
- skill catalog。
- memory candidate / durable memory，当前阶段为 deferred assertion。
- transcript injected notes。
- live terminal process state。

### 0.4 Approval 必须 allowlist

即使 user simulator 输出 `approve`，harness 也必须只批准剧本允许的 tool snapshot。

示例：

- `write_file` 只能写预期路径和预期内容。
- `edit_file` 只能修改预期文件。
- `terminal` 只能执行剧本允许的命令前缀或精确命令。
- `terminal_process.write/submit/kill` 只能在剧本允许的 process_id 与 action 上执行。

若 tool snapshot 不匹配 allowlist：

- harness 不 approve。
- 测试记录为 harness rejection。
- 可选择执行 `deny` 或直接 fail，取决于该轮剧本定义。

### 0.5 Simulator privileged context 的边界

可以给 user simulator：

- 剧本目标。
- 当前轮用户意图。
- 当前可见回复。
- 当前 pending approval 的用户级摘要。
- 它应扮演的用户偏好，例如“谨慎批准写文件，但拒绝敏感命令”。

不建议给 user simulator：

- 原始 event log。
- verifier 断言。
- 代码内部实现细节。
- “为了测试通过你必须说 X” 这类会让它成为配合者的提示。

## 1. 测试资产形态

建议新增：

```text
tests/test_real_llm_dogfood_pr4.py
```

或先放在现有：

```text
tests/test_real_llm_integration.py
```

更推荐单独文件，避免现有 real LLM integration 文件继续膨胀。

测试 marker：

```python
pytestmark = pytest.mark.real_llm
```

运行开关：

```text
PULSARA_RUN_REAL_LLM=1
PULSARA_RUN_DOGFOOD_LLM=1
```

原因：

- 这些测试会比普通 real smoke 慢。
- 会产生多轮真实模型调用。
- 可能创建 skill、跑 terminal、写 workspace 文件。
- 应和普通 real LLM smoke 分开启用。

建议命令：

```bash
set -a && source .env && set +a \
  && PULSARA_RUN_REAL_LLM=1 PULSARA_RUN_DOGFOOD_LLM=1 \
     uv run pytest tests/test_real_llm_dogfood_pr4.py -q
```

## 2. 核心组件

### 2.1 UserSimulator

接口建议：

```python
class UserSimulator(Protocol):
    async def next_action(self, state: DogfoodUserVisibleState) -> DogfoodUserAction: ...
```

输入：

```python
@dataclass(frozen=True, slots=True)
class DogfoodUserVisibleState:
    phase: str
    turn_index: int
    script_step: str
    last_user_message: str | None
    last_assistant_text: str | None
    pending_approval: DogfoodPendingApprovalSummary | None
    session_summary: dict[str, object]
    visible_notes: tuple[str, ...]
```

输出必须是 JSON-compatible action：

```python
@dataclass(frozen=True, slots=True)
class DogfoodUserAction:
    type: Literal["user_message", "approve", "deny", "stop", "finish"]
    text: str | None = None
    approval_id: str | None = None
    tool_call_ids: tuple[str, ...] = ()
    reason: str | None = None
```

LLM simulator 的 prompt 要要求：

- 只输出 JSON。
- 不解释。
- 不调用工具。
- 不伪造 approval id。
- approval id 必须来自 visible pending approval。
- 如果不确定，输出 `finish` 或 `deny`，不要自由发挥批准。

### 2.2 DogfoodHarness

职责：

1. 创建 workspace 和 HostCore session。
2. 配置 Pulsara agent runtime：

```text
trusted_host + on_request + ask
```

3. 每轮把 user message 送入 `session.run_turn()`。
4. 如果出现 pending approval：
   - 向 simulator 展示 summary。
   - simulator 输出 approve/deny/stop。
   - harness 对 tool snapshot 做 allowlist 校验。
   - 执行 `resolve_approval()` 或 `stop_current_turn()`。
5. 收集 evidence。
6. 交给 verifier。

### 2.3 Approval allowlist

建议配置：

```python
@dataclass(frozen=True, slots=True)
class AllowedToolCall:
    tool_name: str
    path_prefixes: tuple[str, ...] = ()
    exact_paths: tuple[str, ...] = ()
    exact_commands: tuple[str, ...] = ()
    command_prefixes: tuple[str, ...] = ()
    content_substrings: tuple[str, ...] = ()
```

V1 简单实现：

- 对 `write_file` / `edit_file` 检查 JSON arguments。
- 对 `terminal` 检查 `command`。
- 对 `terminal_process` 检查 `action`。
- 未匹配 allowlist 的 tool call 不 approve。

### 2.4 Evidence collector

每个 dogfood 测试应返回结构化 evidence：

```python
@dataclass(slots=True)
class DogfoodEvidence:
    turns: list[DogfoodTurnEvidence]
    event_type_counts: dict[str, int]
    run_statuses: list[tuple[str, str | None]]
    approval_events: list[dict]
    tool_calls: list[dict]
    tool_results: list[dict]
    memory_summary: dict[str, object] | None  # 当前 PR4 dogfood 为 None；Phase 3 打开 memory 后再填充。
    skill_catalog_snapshots: list[dict]
    session_summaries: list[dict]
    filesystem_artifacts: dict[str, str]
```

测试失败时应打印压缩 JSON evidence，便于复盘。

## 3. 阶段一：4 轮小测试

目标：先验证 runner/simulator/harness/verifier 结构可靠，不急着覆盖所有 seam。

建议测试名：

```python
def test_real_pr4_dogfood_llm_user_small_loop(tmp_path): ...
```

### 3.1 固定运行策略

Host session：

```text
workspace_kind = project
permission_profile = trusted_host
approval_policy = on_request
terminal_access = ask
model_role = flash
max_output_tokens >= 512
memory_reflection = disabled
```

User simulator：

- 使用 real LLM。
- model role 可用 flash。
- 不给工具。
- 输出 JSON action。

### 3.2 四轮剧本

#### Round 0: Inspect baseline

用户模拟器不参与。

动作：

- 运行 `host inspect` 等价逻辑，或直接调用 inspect helper。

预期：

- policy 显示 `trusted_host + on_request + ask`。
- bundled `pulsara-skill-installer` / `pulsara-skill-creator` 已可被 catalog 看到，若 dogfood runner 选择先 sync。
- 无 pending approval。
- inspect 不写入 runtime event log。

#### Round 1: Read-only project orientation

用户意图：

```text
看看这是什么项目，简短总结一下。
```

运行路径：

- 模型应使用 `read_file` / `search_files`。
- 读工具不 gate。

观察点：

- 无 pending approval。
- no `RequireUserConfirmEvent`。
- 不做 memory assertion。
- final text 包含项目识别信息，例如 Pulsara / agent runtime。

#### Round 2: Write one small artifact with approval

用户意图：

```text
在 dogfood_artifacts/summary.md 写一份两三行的项目摘要。
```

运行路径：

- `write_file` 触发 `approval_policy=on_request`。
- pending approval 出现。
- user simulator 应输出 approve。
- harness 校验 path 精确为 `dogfood_artifacts/summary.md`，content 不含危险内容。
- approve 后同一 run 继续。

观察点：

- `RequireUserConfirmEvent` 1 次。
- `UserConfirmResultEvent` 1 次。
- 文件落地。
- final text 承认已写入。

#### Round 3: Terminal ASK with approve

用户意图：

```text
运行一个很小的命令确认环境：printf PULSARA_DOGFOOD_SMALL_OK
```

运行路径：

- `terminal` 触发 `terminal_access=ask`。
- pending approval 出现。
- simulator 输出 approve。
- harness 只批准 exact command。
- approve 后 terminal 执行。

观察点：

- pending tool name 为 `terminal`。
- terminal result output 含 `PULSARA_DOGFOOD_SMALL_OK`。
- final text 含 sentinel。
- 无 `terminal_process`，避免阶段一被 poll/wait 摩擦干扰。

### 3.3 阶段一通过标准

- 四轮测试稳定通过。
- simulator action JSON 可解析。
- harness allowlist 能阻止非预期 approval。
- `run_turn()`、pending approval、approve、resume、final 都能串起来。
- evidence 输出足够调试失败。

阶段一不要求：

- 多次 suspend/resume。
- skill install。
- terminal yield / terminal_process。
- active stop。
- failure note。
- memory durable correctness。

Memory durable correctness 留给后续 Phase 3；其余进入阶段二。

## 4. 阶段二：0-10 长程剧本

目标：完整压测 PR4 作为常态路径时的真实产品体验。

建议测试名：

```python
def test_real_pr4_dogfood_llm_user_long_session(tmp_path): ...
```

阶段二默认打开：

- `trusted_host + on_request + ask`
- bundled skills sync
- workspace skills
- durable memory / reflection disabled
- terminal tools
- approval resume
- user stop

### Round 0: host inspect baseline

用户意图：

- 无 agent 用户消息。

运行路径：

- inspect only。

观察点：

- policy 显示 `trusted_host + on_request + ask`。
- bundled `pulsara-skill-installer` / `pulsara-skill-creator` 已 sync / 可见。
- 无 pending approval。
- 无 stopping run。
- inspect 只读。

### Round 1: 项目认知

用户意图：

```text
看看这是什么项目。
```

运行路径：

- `read_file` / `search_files`。
- 读工具不触发 approval。
- skill catalog 渲染。

观察点：

- 无审批弹出。
- catalog 能看到 bundled skills。
- 不做 memory assertion；当前 long dogfood 不打开 durable memory。

### Round 2: 创建一个测试失败诊断 skill

用户意图：

```text
造一个跑测试并总结失败的 skill。
```

运行路径：

- 激活 `pulsara-skill-creator`。
- 写入 `.pulsara/skills/<skill-name>/SKILL.md`。
- 写入 `scripts/...`。
- 可选写 `references/...`。

关键要求：

- 提示词应诱导“一次只写一个文件，看到结果后再写下一个文件”，从而真实压测一轮内多次 suspend/resume。
- 每个 `write_file` 都会 pending。
- 每个 approve 都由 harness allowlist 校验。

观察点：

- 一轮内多次 `RequireUserConfirmEvent` / `UserConfirmResultEvent`。
- 多次 suspend/resume 后仍同一 run 继续。
- 文件落地。
- skill 当前轮不应被立刻用于 capability resolve；下一轮才可见。
- 不做 memory assertion；当前 long dogfood 只检查文件与 skill catalog。

### Round 3: 使用新 skill 检查项目

用户意图：

```text
用这个新 skill 检查项目。
```

运行路径：

- 新 skill 在本轮 capability resolve 中可见。
- skill 指导模型跑测试。
- `terminal` 因 ASK 进入 approval。
- 当前测试要求不使用 `terminal_process`，避免把 PR4 approval dogfood 和 process polling UX 摩擦绑在一起。

观察点：

- skill catalog：上一轮不在，本轮在。
- terminal call pending -> approve。
- pytest 输出被捕获。
- 若有测试失败，模型能总结失败。
- 不期待 `terminal_process.poll/wait`；相关 UX 摩擦留给后续专门 dogfood。

### Round 4: 跑集成测试并中途 stop

用户意图：

```text
顺便把集成测试也全跑了。
```

运行路径：

- agent 发起较长 terminal command。
- harness 在模型进入 active run 后调用 `stop_current_turn()`。

观察点：

- active run soft stop。
- canonical `RunEndEvent(status="aborted", stop_reason="aborted")`。
- session 回 idle。
- `_run_lock` 释放。
- 当前测试不制造后台进程；soft cancel 验证集中在 active run 状态与 transcript note。

### Round 5: Continue after stop

用户意图：

```text
继续刚才的。
```

运行路径：

- prior messages 包含 interrupted note。

观察点：

- context 中出现 `INTERRUPTED_NOTE_TEXT`。
- 模型不假装上轮已经完成。
- 模型从保留输入继续或解释需要重新执行。

### Round 6: Sensitive action pending, user stops instead of approve/deny

用户意图：

```text
帮我做一个可能涉及敏感文件的检查。
```

运行路径：

- agent 提议敏感 terminal action 或写入敏感路径。
- pending approval 出现。
- user simulator 输出 `stop`，不是 approve/deny。

观察点：

- suspended approval stop。
- run aborted。
- pending tool 未执行。
- pending approval 清空。
- 不生成 denied tool result，因为 stop != deny。

### Round 7: Retry and deny

用户意图：

```text
重试一下，但这次我不允许这个动作。
```

运行路径：

- agent 再次提出同类 action。
- pending approval 出现。
- user simulator 输出 deny。

观察点：

- `UserConfirmResultEvent(confirmed=false)`。
- denied tool result 进入 context。
- 同一 run 继续。
- 模型解释被拒绝或换方案。
- final 承认用户拒绝。

### Round 8: Inject controlled failure

用户意图：

- 由 harness 注入 controlled provider / runtime failure。

建议：

- 不依赖真实供应商抖动。
- 用 synthetic failed run path 产生 `RunEndEvent(status="failed")`，并维护 publisher sequence，不发布 synthetic events。

观察点：

- 下一轮 prior context 看到 failure note。
- 关键：failure note 作为最新 terminal run note，取代旧 interrupted note。
- 只出现一条 note。
- 不泄露 raw provider stack / API key。

### Round 9: Failure-note continuation

用户意图：

```text
刚才失败后发生了什么？还能继续吗？
```

运行路径：

- prior context / transcript projection。

观察点：

- Round 8 failed 后，下一轮 prior messages 含 failure note。
- failure note 作为最新 terminal run note，取代旧 interrupted note。
- 模型能识别上一轮失败并按“继续/说明失败”的语义回复。
- 这一轮不验证 memory recall。

### Round 10: Final inspect

用户意图：

- 无 agent 用户消息。

运行路径：

- inspect only。

观察点：

- bundled skills 与 installed workspace skill 共存。
- 无 pending approval。
- 无 `stopping_run_id`。
- session summary 干净。
- 当前测试不要求 live process 残留，因为剧本不使用 `terminal_process`。

## 5. 重点 seam 清单

### 5.1 一轮内多次连续审批

Round 2 必须真实触发多次：

```text
suspend -> approve -> resume -> suspend -> approve -> resume
```

要观察：

- 每次 approval 是否对应正确 tool call snapshot。
- 多次 `UserConfirmResultEvent` 是否挂到正确 run/reply。
- `begin_next_turn()` 时机是否正确。
- final run 只发一次 terminal `RunEndEvent`。

### 5.2 Skill 可见性时机

Round 2 创建 skill，Round 3 才可见。

要观察：

- capability resolver per-turn 工作正常。
- registry per-session 不会动态新增 tool，这个不对称应被记录，而不是误判 bug。

### 5.3 terminal_process ASK 摩擦（后续）

当前 long dogfood 明确避免 `terminal_process`。后续若增加 yielded process 剧本，需要观察：

- `terminal_process.poll/wait` 也会 approval。
- 记录交互成本。
- 如果难用到明显阻碍 dogfood，后续考虑 action-level policy：
  - gate `write/submit/kill`
  - allow `poll/wait`

### 5.4 Soft stop

Round 4 观察：

- active run abort。
- session idle。
- `_run_lock` 释放。
- 后续 turn 能继续。
- 当前测试不制造后台进程，因此不验证进程回收或 inspect live-process 展示。

### 5.5 Transcript notes 叠加

Round 5 / 8 检查：

- interrupted note 出现在 stop 后下一轮。
- failure note 出现在 failed 后下一轮。
- newer terminal note supersedes older note。
- 不重复注入多条 note。

### 5.6 Memory 不污染（Phase 3）

当前 PR4 long dogfood 不打开 durable memory / reflection，因此 Round 9 不做 memory recall 或污染审计。后续 Phase 3 可补独立测试：

- aborted run 不进入 durable memory。
- failed run 不进入 durable memory。
- approval note / system note 不被当成用户偏好。
- denied action 不被记成用户已经做过。

## 6. Verifier 断言矩阵

阶段一最小断言：

- Round 0 inspect policy 正确。
- Round 1 no approval。
- Round 2 write_file approval -> approve -> file exists。
- Round 3 terminal approval -> approve -> sentinel output。
- session final summary 无 pending。

阶段二完整断言：

- Round 0 / 10 inspect 只读且状态一致。
- Round 2 至少 3 次 write approval。
- Round 3 skill catalog 出现新 skill。
- Round 3 terminal approval 成功。
- Round 4 active stop -> aborted。
- Round 5 interrupted note visible。
- Round 6 suspended stop -> aborted, no tool result。
- Round 7 deny -> denied tool result, same run continues。
- Round 8 failed -> failure note visible, interrupted note superseded。
- Round 9 failure-note continuation -> final text 命中 failure-note sentinel。

## 7. 实施切分

### PR A: Small loop dogfood asset

- 新增 `tests/test_real_llm_dogfood_pr4.py`。
- 新增 UserSimulator abstraction。
- 新增 deterministic scripted simulator fallback。
- 新增 real LLM simulator implementation。
- 新增 4 轮小测试。
- 默认 skip，需 `PULSARA_RUN_REAL_LLM=1 PULSARA_RUN_DOGFOOD_LLM=1`。

### PR B: Long session dogfood asset

- 扩展同一 runner 支持 0-10 剧本。
- 增加 skill creation/install/use。
- 增加 active stop。
- 增加 suspended stop / deny。
- 增加 controlled failure injection。
- 增加 failure-note continuation。

### PR C: Memory dogfood audit

作为后续独立阶段，而不是当前 PR4 long dogfood 的一部分：

- 打开 durable memory / reflection。
- 设计 memory recall 用户轮：“我们做了什么？你记住了我哪些偏好？”
- 验证 Observation / Decision / Preference 累积合理。
- 验证 aborted / failed / denied 不被误抽成 durable fact 或长期偏好。

### PR D: Optional UX findings

根据 dogfood 结果决定是否开后续小 PR：

- terminal_process action-level approval。
- one-shot approval prompt。
- richer inspect live-process summary。
- dogfood evidence HTML/Markdown report。

## 8. 通过标准

阶段一完成标准：

- 小测试能稳定跑通。
- harness 能安全 approve。
- evidence 失败时可读。
- 不依赖手工操作。

阶段二完成标准：

- 长程剧本在真实 LLM 下可运行。
- 所有 verifier 断言通过，或明确标记为 product gap。
- 至少输出一份结构化 dogfood report。
- 测试资产进入 `tests/`，默认 skip，按 env opt-in。

## 9. Non-goals

- 不用 LLM simulator 判定 pass/fail。
- 不让 simulator 绕过 harness allowlist。
- 不追求完全 deterministic 的自然语言输出。
- 不把 dogfood runner 做成产品 UI。
- 不在这轮实现 durable approval store。

## 10. 设计判断

LLM user simulator 的价值是制造真实交互压力，而不是替代测试断言。

真正的安全感来自三层组合：

1. LLM simulator 产生自然用户行为。
2. harness 只执行被 allowlist 证明安全的动作。
3. verifier 从 event log / filesystem / session summary 判定当前 PR4 dogfood 是否满足契约；Phase 3 再把 memory 纳入判定。

这样 dogfood 才既像真实使用，又能在失败时给出可复盘证据。
