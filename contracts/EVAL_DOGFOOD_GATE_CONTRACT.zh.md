# Eval / Core Real-Provider Dogfood Gate Contract

_Created: 2026-07-04_

本文档定义 Pulsara eval、deterministic gate 与 real-LLM dogfood 的契约。它不是“测试建议”，而是发布前如何证明 memory / runtime / tool 行为没有倒退的最低事实边界。

相关代码：

- [evals/recall/runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/recall/runner.py)
- [evals/recall/config.yaml](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/recall/config.yaml)
- [evals/governance_relatedness/runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/governance_relatedness/runner.py)
- [evals/governance_relatedness/config.yaml](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/governance_relatedness/config.yaml)
- [tests/test_recall_eval_runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_recall_eval_runner.py)
- [tests/test_governance_relatedness_eval_runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_governance_relatedness_eval_runner.py)
- [benchmarks/suites/README.md](/Users/plumliu/Desktop/python_workspace/pulsara_agent/benchmarks/suites/README.md)
- [benchmarks/suites/core/v1/manifest.json](/Users/plumliu/Desktop/python_workspace/pulsara_agent/benchmarks/suites/core/v1/manifest.json)

---

## 1. 核心立场

Pulsara 的发布门槛分两层：

1. **Deterministic eval / unit / integration tests**
   - 默认 CI 可跑；
   - 不依赖外部 LLM provider；
   - 是 PR 级阻塞门槛。
2. **Real-LLM dogfood**
   - 需要显式环境变量；
   - 允许 provider 方差；
   - 是触及 LLM/tool/retrieval/memory 主线时的发布前证据，不是普通 CI 默认项。

不能把“真实模型跑过一次”当成 deterministic correctness proof；也不能因为 deterministic tests 绿就声称 provider trajectory 已验证。

---

## 2. Deterministic recall eval

### 2.1 Fixture 与 baseline

Recall eval 使用 versioned fixture：

- `evals/recall/fixtures/v1_golden.jsonl`
- `evals/recall/fixtures/v2_semantic.jsonl`

`evals/recall/config.yaml` 必须记录：

- runner version；
- fixture path；
- floor path；
- baseline kind；
- golden set sha；
- metric blocking phase。

Fixture hash 与 baseline/config 必须一致；否则 eval floor 不可信。

### 2.2 Metrics

Recall deterministic report 至少包含：

- included hit rate；
- excluded leak count；
- rejected leak count；
- superseded leak count；
- confabulation count；
- p95 latency；
- projection over-budget count；
- failures / informational。

`gate_passed` 只能在没有 blocking failures 时为 true。

### 2.3 Semantic recall hole 必须被测

仅靠 lexical/token overlap 不足以证明 semantic recall。

因此：

- semantic-only fixture 必须存在；
- fixture dense candidate path 必须可测；
- high-threshold style miss 不能被低 noise 掩盖。

---

## 3. Governance relatedness eval

### 3.1 Config 是 gate truth

`evals/governance_relatedness/config.yaml` 是 gate 配置真源。

必须包含：

- fixture version；
- fixture path；
- relatedness policy version；
- alias policy version；
- embedding fingerprint；
- embedded text builder version；
- candidate limit；
- rerank top-m；
- dense/rerank threshold；
- inline gap embedding cap；
- gates block。

Runner 不得 hardcode gate，必须加载 config。

### 3.2 Required gates

Relatedness gate 至少包含：

- `overall_recall_at_k_min`
- `positive_slice_recall_at_k_min`
- `overall_miss_rate_max`
- `destructive_action_false_positive_max`

当前 v1 默认：

```yaml
overall_recall_at_k_min: 0.95
positive_slice_recall_at_k_min: 0.90
overall_miss_rate_max: 0.05
destructive_action_false_positive_max: 0
```

### 3.3 Precision 与 recall 必须同时守护

Relatedness eval 不能只测 destructive-action precision。

必须同时报告：

- recall@k；
- miss rate；
- per-slice recall；
- irrelevant candidate count；
- destructive false-positive count。

否则 threshold 调高会重新制造 semantic recall hole，而测试可能仍然因 false positive 低而通过。

### 3.4 Planner action prediction 必须存在

`--gate` 模式必须 fail-closed：

- 有 candidate predictions 但没有 destructive action predictions -> gate fail；
- destructive false positive 超过上限 -> gate fail；
- hard-negative candidate noise 不等价于 destructive false positive。

---

## 4. Real-LLM dogfood

### 4.0 冻结的核心 suite 是发布真源

Real-LLM 发布门禁的长期真源是：

```text
benchmarks/suites/core/v1
```

它与 `evals/` 的边界是：

- `evals/` 保存可重复的质量数据集与 deterministic grader；
- `benchmarks/suites/` 保存会启动真实 Pulsara、真实 provider、真实本地 durable
  services 和 workspace tools 的昂贵产品轨迹。

Core v1 固定六条轨迹：

1. ordinary workspace patch；
2. append-only provider cache continuity；
3. cross-process durable resume；
4. explicit long-horizon compaction；
5. subagent delegation/result delivery；
6. typed Plan question/approval/implementation。

每条轨迹必须：

- 在独立工作区运行；
- 不把 hidden verifier 暴露给模型；
- 使用 `HostCore` / `HostSession` 产品边界，不手工构造 durable events；
- 会话 close drain 后再由 `InspectorService` 读取证据；
- 保存 suite、scenario、runner fingerprint；
- 同时通过 workspace verifier 与 durable lifecycle grader。

Core suite 必须串行运行。并行真实 provider trajectory 会让 PostgreSQL、provider cache、
限流和成本证据难以归因，不属于 v1 合法执行模式。

旧 real-LLM pytest 已物理删除。pytest 只承载离线 correctness tests 和独立的
`retrieval_live` API 检查；任何新的真实 Agent trajectory 必须进入版本化 core suite，
不得重新建立按功能散落的联网 pytest fixture。

### 4.1 显式执行门禁

Core suite 默认拒绝联网执行。离线 `validate` / `list` 不读取 API key；真实执行必须同时
提供环境变量和命令行确认：

```bash
PULSARA_RUN_CORE_DOGFOOD=1 \
uv run python -m benchmarks.suites.run_core_dogfood run \
  --env-file .env \
  --confirm-network
```

缺少 provider key、Postgres、Oxigraph 或其它场景依赖时，runner 必须在 preflight 或场景
结果中给出明确失败原因；不得把缺失发布证据静默解释为通过。

### 4.2 Real dogfood 证明什么

Real-LLM dogfood 证明的是 provider-facing trajectory：

- 真实模型是否会按 prompt 使用正确工具；
- 中转站 / provider 是否会填默认参数或空语义参数；
- tool schema fallback 是否稳健；
- LLM transport 是否兼容 provider event stream；
- append-only provider input 是否真的形成可缓存前缀；
- real plan/resume/compaction trajectory 是否可恢复；
- subagent result delivery 与 workspace tool execution 是否闭环。

它不证明：

- 所有 provider 都稳定；
- 所有随机 trajectory 都可重复；
- deterministic safety gate 可以移除。

### 4.3 Dogfood 必须输出可审计证据

Real dogfood 不应只断言 final text。

根据场景，报告必须包含可审计事实，例如：

- tool names / tool call count；
- final status；
- recall traces；
- vector row count；
- usage count；
- resources closed；
- timeline / outbox lane；
- projection ids；
- hidden id 未泄漏；
- pending interaction count；
- artifact id / compaction id；
- downloaded / deleted file evidence。

---

## 5. Core trajectory coverage

Core v1 的覆盖责任固定如下：

- `workspace-patch`：Host tool loop、文件修改、隐藏 verifier；
- `cache-continuity`：连续三轮 strict-suffix input 与 provider cache telemetry；
- `durable-resume`：detach、进程替换、durable reopen；
- `manual-compaction-trail`：长证据链、显式 compaction、无需重读的召回；
- `subagent-delegation`：child task、result delivery、parent isolation；
- `plan-workflow`：typed question、exit、approval 与 implementation continuation。

Memory recall 和 governance relatedness 的质量 floor 继续由第 2、3 节的 versioned
deterministic eval 负责。`retrieval_live` 只验证 retrieval provider API compatibility，
不是 Agent trajectory，也不能替代 core suite。

---

## 6. 禁止事项

- 不允许用 root design doc 中的手写指标替代 eval config。
- 不允许 `--gate` 在缺 planner destructive-action predictions 时通过。
- 不允许只看 precision，不看 recall@k / miss rate。
- 不允许把 real LLM dogfood 加进默认快速 CI。
- 不允许把 provider 一次通过当成长期兼容保证。
- 不允许跳过隐藏 scope 泄漏断言。
- 不允许 real dogfood 只检查 final answer 文本而无 trace/evidence。

---

## 7. 最低命令

Deterministic gates：

```bash
uv run pytest tests/test_recall_eval_runner.py tests/test_governance_relatedness_eval_runner.py -q
```

Recall gate entrypoint：

```bash
uv run python evals/recall/runner.py \
  --fixture evals/recall/fixtures/v1_golden.jsonl \
  --gate --json
```

Governance relatedness gate entrypoint：

```bash
uv run python evals/governance_relatedness/runner.py \
  --fixture evals/governance_relatedness/fixtures/v1_semantic.jsonl \
  --predictions <structured-predictions.json> \
  --gate
```

Core dogfood entrypoint：

```bash
uv run python -m benchmarks.suites.run_core_dogfood validate

PULSARA_RUN_CORE_DOGFOOD=1 \
uv run python -m benchmarks.suites.run_core_dogfood run \
  --env-file .env \
  --confirm-network
```

Real dogfood 命令不得被写成默认 CI 必跑项，除非 CI 环境明确提供 provider、Postgres、Oxigraph、keys 与成本预算。

---

## 8. Long-horizon trajectory gate

Long-Horizon 的发布轨迹由 `manual-compaction-trail` 冻结。它必须证明显式 rewrite
authority、summary artifact、window transition、close drain，以及 compaction 后无需重读原始
长证据仍能完成隐藏 verifier。更细的 pairing、settlement、mid-turn cancellation 和
fake/PostgreSQL parity 继续由默认离线测试覆盖；不得为每个内部边界重新增加联网 pytest。
