# Eval / Real-LLM Dogfood Gate Contract

_Created: 2026-07-04_

本文档定义 Pulsara eval、deterministic gate 与 real-LLM dogfood 的契约。它不是“测试建议”，而是发布前如何证明 memory / runtime / tool 行为没有倒退的最低事实边界。

相关代码：

- [evals/recall/runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/recall/runner.py)
- [evals/recall/config.yaml](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/recall/config.yaml)
- [evals/governance_relatedness/runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/governance_relatedness/runner.py)
- [evals/governance_relatedness/config.yaml](/Users/plumliu/Desktop/python_workspace/pulsara_agent/evals/governance_relatedness/config.yaml)
- [tests/test_recall_eval_runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_recall_eval_runner.py)
- [tests/test_governance_relatedness_eval_runner.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_governance_relatedness_eval_runner.py)
- [tests/test_real_llm_memory_recall_dogfood.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_real_llm_memory_recall_dogfood.py)
- [tests/test_real_llm_governance_relatedness.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_real_llm_governance_relatedness.py)
- [tests/test_real_llm_integration.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_real_llm_integration.py)
- [tests/test_real_llm_context_compaction.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_real_llm_context_compaction.py)
- [tests/test_real_llm_resume.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_real_llm_resume.py)
- [tests/test_real_llm_hf_cli_skill_dogfood.py](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_real_llm_hf_cli_skill_dogfood.py)

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

### 4.1 Opt-in 环境变量

Real-LLM tests 默认必须 skip。

最低开关：

```text
PULSARA_RUN_REAL_LLM=1
```

更昂贵或更具外部依赖的 dogfood 必须有额外开关，例如：

- `PULSARA_RUN_DOGFOOD_LLM=1`
- `PULSARA_RUN_DOGFOOD_LONG=1`
- `PULSARA_RUN_DOGFOOD_PLAN_LONG=1`
- `PULSARA_RUN_DOGFOOD_COMPACTION=1`
- `PULSARA_RUN_DOGFOOD_COMPACTION_LONG=1`
- `PULSARA_RUN_DOGFOOD_COMPACTION_MID_TURN=1`
- `PULSARA_RUN_HF_CLI_DOGFOOD=1`

缺少 provider key、embedding/rerank key、Postgres/Oxigraph 等外部服务时，dogfood 可以 skip，但必须给出明确 skip reason。

### 4.2 Real dogfood 证明什么

Real-LLM dogfood 证明的是 provider-facing trajectory：

- 真实模型是否会按 prompt 使用正确工具；
- 中转站 / provider 是否会填默认参数或空语义参数；
- tool schema fallback 是否稳健；
- LLM transport 是否兼容 provider event stream；
- real embedding/rerank 是否能召回 semantic target；
- real plan/resume/compaction trajectory 是否可恢复；
- active skill + terminal 是否能完成真实 CLI 任务。

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

## 5. Memory recall long dogfood

Long memory recall dogfood 是 semantic recall 发布证据之一。

最低断言：

- vector index materialized row count 符合 seed；
- retrieval resources closed；
- all turns finished；
- automatic recall 仅在相关 user turn 注入；
- explicit `memory_search` 命中目标 memory id；
- hidden scope memory 不出现在 projection / tool result；
- unrelated negative 不调用 memory_search；
- explicit traces 有 `trigger_kind=explicit_search`；
- explicit traces 包含 reranker / vector metadata；
- usage rows 产生。

这类 dogfood 需要 real embedding + rerank provider；缺 key 时 skip 是合法的。

---

## 6. Real governance relatedness dogfood

Real relatedness dogfood 用真实 embedding/reranker 跑 versioned fixture。

最低断言：

- overall recall@k >= config floor；
- miss rate <= config floor；
- positive slice recall >= config floor；
- batch embedding 调用被批量化，而非 per-candidate N 次；
- hard-negative 不误授权 destructive lifecycle。

真实 dogfood 可以只覆盖当前 lifecycle-enabled 类型路径；其它类型必须由 deterministic tests 补足。

---

## 7. Plan / resume / compaction / CLI skill dogfood

这些 dogfood 属于 runtime trajectory 证据：

- plan dogfood：必须覆盖 structured question、exit_plan、approve/resume、pending interaction 清空。
- resume dogfood：必须覆盖 durable thread reopen 后上下文仍可用。
- compaction dogfood：必须覆盖 summary artifact、boundary replay、manual/mid-turn safe point。
- HF CLI skill dogfood：必须覆盖 active skill injection、terminal usage、真实下载、删除、cleanup。

它们的 gating 应按改动范围选择，不得要求每个普通 PR 都运行全部真实 dogfood。

---

## 8. 禁止事项

- 不允许用 root design doc 中的手写指标替代 eval config。
- 不允许 `--gate` 在缺 planner destructive-action predictions 时通过。
- 不允许只看 precision，不看 recall@k / miss rate。
- 不允许把 real LLM dogfood 加进默认快速 CI。
- 不允许把 provider 一次通过当成长期兼容保证。
- 不允许跳过隐藏 scope 泄漏断言。
- 不允许 real dogfood 只检查 final answer 文本而无 trace/evidence。

---

## 9. 最低命令

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

Representative real dogfood examples：

```bash
PULSARA_RUN_REAL_LLM=1 \
uv run pytest tests/test_real_llm_memory_recall_dogfood.py::test_real_llm_long_memory_recall_dogfood -q -s

PULSARA_RUN_REAL_LLM=1 \
uv run pytest tests/test_real_llm_governance_relatedness.py::test_real_embedding_reranker_relatedness_fixture_recall_and_noise -q -s
```

Real dogfood 命令不得被写成默认 CI 必跑项，除非 CI 环境明确提供 provider、Postgres、Oxigraph、keys 与成本预算。
