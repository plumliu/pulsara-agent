# Subagent Profile 删除验收记录

日期：2026-10-30。

## 验收结论

已按当前实施规格删除 Subagent profile 的工具参数、请求值、存储列、查询字段和模型输入 entry。新建 Work 的真实启动组覆盖初始 context、instruction、依赖材料的顺序；复用 Work 的真实压缩测试确认当前 RUNNING turn 采用 revision 1，摘要截止在当前输入组之前，队列 consumed entry 指向 instruction，后续 Agent 消息仍到达首个普通模型输入。会话复用保留已安装的 SYSTEM、tools 和 messages 前缀。

委派输入的真实 Host 测试验证图片路径保持文本、启动组没有图片引用，Subagent 主动调用 `view_image` 后工具结果才向模型提供图片。合成 canonical 图片测试只验证 reader／planner 的投影行为，不作为委派图片入口的证据。fork 精确复制、ACK 丢失不重复复制和 CODE_RESULT 的 provider／预算过滤也在联合回归中覆盖。

原复用压缩测试只证明发起摘要，未证明采用；原 fixture 把大型 final 同时作为当前 Work 必须保留的材料。调整为较长的前驱 instruction 和短 final 后，新断言确认 revision 0→1、组前截止和消费身份。调试测量显示摘要前输入 19,773 tokens、后继输入 11,908 tokens，目标约 16,717 tokens；原 fixture 的未采用原因未捕获，不能作为已测得数值。

## 验证结果

PostgreSQL 测试使用已核实的本地 PostgreSQL 测试服务（socket 或 localhost，具体 DSN 见各命令）和 fixture 创建的独立临时数据库；未重置原数据库。以下回归之间有重叠，不将测试数量相加。

- 跨链联合回归：78 passed，15 subtests passed，21.99 秒。精确命令如下，两个 DSN 为 `host=/Users/plumliu/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin` 和 `host=/Users/plumliu/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_runtime`：

```sh
PULSARA_POSTGRES_ADMIN_DSN='host=/Users/plumliu/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_admin' PULSARA_POSTGRES_DSN='host=/Users/plumliu/.pulsara/desktop/socket port=5432 dbname=pulsara user=pulsara_runtime' .venv/bin/python -m pytest -q \
  tests/test_agent_collaboration_api.py \
  tests/test_builtin_default_null_contracts.py \
  tests/test_agent_collaboration_postgres.py::test_public_batch_atomic_rejection_and_wait_reads_exact_real_finals \
  tests/test_agent_collaboration_postgres.py::test_public_admission_confirms_a_real_commit_after_acknowledgement_loss \
  tests/test_agent_code_mode_execution.py::test_batch_hooks_reject_invalid_rewrites_and_keep_accepted_work_after_post_failure \
  tests/test_agent_requests_persistence.py::test_full_results_are_owned_and_ordered_before_first_model_input \
  tests/test_agent_requests_persistence.py::test_waiting_work_is_not_idle_and_cancel_preserves_other_work \
  tests/test_canonical_request_start_postgres.py::test_queued_group_keeps_context_instruction_and_material_identity \
  tests/test_agent_context_canonical.py::test_reused_agent_reads_ordinary_history_and_exact_group_cut \
  tests/test_agent_context_canonical.py::test_code_results_are_visible_to_ui_but_filtered_before_provider_budget_search_and_fork \
  tests/test_agent_context_canonical.py::test_code_results_do_not_consume_canonical_or_compaction_budget \
  tests/test_agent_context_input.py::test_started_group_distinguishes_instruction_suffix_end_and_predecessor \
  tests/test_agent_context_input.py::test_agent_group_has_independent_ordered_user_messages_and_instruction_compaction \
  tests/test_agent_context_input.py::test_reused_agent_appends_instruction_and_materials_within_same_epoch \
  tests/test_compaction_first_call_source_boundary.py::test_first_call_summary_ceiling_precedes_the_whole_initial_group \
  tests/test_compaction_first_call_source_boundary.py::test_destination_summary_excludes_context_instruction_and_dependency_material \
  tests/test_compaction_first_call_source_boundary.py::test_new_turn_adopted_base_keeps_exact_independent_initial_entries \
  tests/test_compaction_first_call_source_boundary.py::test_adoption_lost_ack_confirms_full_without_reissuing_write \
  tests/test_compaction_request_start_integration.py::test_subagent_first_summary_uses_full_initial_group_and_current_work \
  tests/test_agent_session_host.py::test_same_agent_uses_real_common_engine_and_retains_prefix_across_work \
  tests/test_agent_session_host.py::test_agent_image_path_stays_text_until_real_view_image_tool \
  tests/test_conversation_fork.py::test_fork_commit_ack_loss_confirms_preselected_child_without_recopy \
  tests/test_conversation_fork.py::test_fork_copies_exact_prefix_with_no_execution_and_continues
```

- Clean-v0 与存储：`PULSARA_POSTGRES_ADMIN_DSN='host=localhost port=5432 dbname=pulsara user=plumliu' PULSARA_POSTGRES_DSN='host=localhost port=5432 dbname=pulsara user=pulsara' .venv/bin/python -m pytest tests/test_stage5_clean_migration.py tests/test_agent_requests_persistence.py tests/test_agent_collaboration_postgres.py tests/test_canonical_request_start_postgres.py -q --tb=short`：37 passed，10.84 秒；包括 fresh install、重复 migrate、catalog 校验、合法来源和已删来源 CHECK。
- 模型输入与图片：`.venv/bin/python -m pytest -q tests/test_agent_context_input.py tests/test_compaction_first_call_source_boundary.py tests/test_kernel_image_input_k3.py`：95 passed，42.06 秒。
- Host 与 Hook：使用上方本地 socket DSN，`.venv/bin/python -m pytest -q tests/test_agent_session_host.py tests/test_agent_session_runtime.py tests/test_hook_input_identity.py tests/test_runtime_handoff_agent_postgres.py`：23 passed，32.49 秒。随后增加图片 SQL 断言，单独重跑 `tests/test_agent_session_host.py::test_agent_image_path_stays_text_until_real_view_image_tool`：1 passed，2.64 秒；该最终版本也包含于联合回归。
- 共享工具合同：`.venv/bin/python -m pytest -q tests/test_agent_collaboration_api.py tests/test_builtin_default_null_contracts.py tests/test_agent_code_mode_capability_consumers.py tests/test_agent_code_mode_execution.py -m 'not postgres'`：77 passed，11 deselected，15 subtests passed，1.75 秒。PG 的 batch Hook 改写测试：4 passed，9.11 秒。
- 前端：在 `frontend` 运行 `npm run build:desktop` 和 `./node_modules/.bin/tsc --noEmit --incremental false` 成功；`npm test -- ../tests/frontend/runtime-adapter.test.ts`：111 passed。构建有 Vite 大 chunk 警告。资源引用检查通过；静态桌面资源与 adapter 中无五种旧预设或 `AGENT_PROFILE`。
- Hook matcher：`.venv/bin/python -m pytest tests/test_round9_2_hook_subsystem.py -k matcher -q`：2 passed，41 deselected。
- Dogfood 和 bundled skills：`.venv/bin/python -m compileall -q tests/dogfood/run_pr03_user_control_dogfood.py tests/dogfood/run_pr04_prompt_queue_dogfood.py` 及这些文件和其共享消费者的 import 检查通过；`.venv/bin/python -m pytest tests/test_bundled_skills.py -q`：12 passed。退役 Task API 的 Round10 脚本与 PR03/PR04 对应退役场景已删除，其他历史脚本未作为本次通过证据。
- 最终统一检查：`git diff --name-only --diff-filter=ACM -- '*.py' | xargs .venv/bin/ruff check` 通过；`git diff --check` 通过。

生产 Python 静态 AST 检查解析 362 个文件，未发现 `AGENT_PROFILE`、`effective_profile`、`profile_text`、`subagent_profile_text` 标识符；针对五种旧预设及上述来源和列的生产源码 `rg` 无匹配。测试中的旧字面量用于拒绝旧参数、模拟投影清除及验证 schema 禁止，不代表兼容路径。模型 reasoning/wire profile 与 Code Mode 资源 profile 保持原职责。

本次未运行真实 provider dogfood；规格将其列为条件验证，真实 Host 与 PostgreSQL 定向及联合回归已完成。未运行全仓测试，结果仅覆盖上述明确范围。
