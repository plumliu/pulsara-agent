# Kernel 图片输入 K4 验收记录

日期：2026-09-15。权威：`AGENTS.md`、`PULSARA_KERNEL_IMAGE_INPUT_AND_OPENAI_WIRE_ADAPTER_DESIGN.zh.md` 第 12 节 I01–I44 与当前生产代码。

状态：**K4 Kernel 验收通过**。最终根 `.venv` 完整 non-live 回归（包含 PostgreSQL）**1979 passed / 19 warnings / 416.93 s，正常退出 0**；修复后的独立 wheel 完成 Chat 与 DeepSeek Responses 共 **18 次正式调用**。K1–K3 验收前提交为 `b24daae8`，本记录覆盖其后的 K4 修复与验收证据。D1–D4 保持冻结，U1/U2 浏览器产品尚未实施。

## 分工和证据

主 agent 负责正式 Host 图片 dogfood、实际 HTTP 输入核对、isolated wheel、I43 生命周期与交叉复核；既有 GPT-5.6-sol/max coding subagent 负责完整 pytest、I01–I44 对照、数据库/并发/取消矩阵、旧 fixture 迁移和发现的生产缺口修复。

图片 dogfood 使用 [`tools/run_kernel_image_input_dogfood.py`](tools/run_kernel_image_input_dogfood.py)，操作说明见[同名文档](tools/run_kernel_image_input_dogfood.md)。图片及完整实际请求/回复仅保存到已忽略的 `scratch/k4-acceptance/`；不提交这些中间证据。dogfood 本身只读加载保存配置，不将模型密钥导出到环境。本轮后段用户明确授权新增 DeepSeek Responses 配置，主 agent 单独通过 `LocalSettingsStore.add_model_connection()` 完成；原 Chat 配置保留。

## 已发现的问题

1. 完整非 live pytest 首轮为 **1948 passed / 21 failed**。部分旧 repository fixture 没有迁移到 K3 必需的 `provider_input_admission`；必须传入对应候选，保留原事务和冲突断言。测试数不替代下方逐项契约核对。
2. 首次 ROOT prospective dispatch 已在 writer 前完成，但原 `SessionStart` Hook 位于被跳过的后置准备块，导致 startup/resume 上下文缺失。修复为先冻结无 Hook 的完整候选，在压缩/交接边界确定后复用原 Hook sibling；最终候选重新通过完整预算，沿原 reservation 生命周期转交。这样保留 compact 对 startup/resume 的 supersession，不重复捕获全部来源。
3. pending ROOT 压缩 idle predecessor 时，Host 仍将新 ROOT 的 active turn ID 与旧 predecessor 比较，拒绝合法的 resume 首次调用压缩。修复在现有栅栏中传递 exact pending turn，保留 owner task、admitted writer 与排他检查；错误 ID、foreign writer 和非 owner 均有拒绝回归。
4. I43 新生命周期测试在 Chat 与 Responses 下均证明省略后的图片不复活，但原 direct FULL-confirm 在合法压缩后返回 CONFLICT。修复删除对可变 current binding revision 的比较；原提交的 immutable initial revision、正文、refs、command、event/model binding 仍完整核对。

## 真实 provider 与安装包

修复后的最终通过组合为已保存的 `openai/gpt-5.6-luna`（Chat Completions）与用户本轮授权新增的 `deepseek-flash`（Responses）。`gpt-5.5`（Responses）保留早前通过及最终两次失败的证据；没有调用排除的内网 `Qwen3.8-27B`。这些结果只证明所记录的配置，不推导所有兼容服务均已验证。

两张合成 PNG 分别显示 `756` 与 `691`。纯图答复为 `756`；交错并重复后的预期为 `{"alpha":"691","bravo":"756","charlie":"691"}`，文字请求不透露数字。工具请求实际读取临时工作区 `marker.txt` 并返回 `K4_TOOL_PINE`。随后验证 summary 读取原图、successor 保留原图、冷恢复读取原图，以及父会话关闭后 fork 子会话继续读取原图。

每组正常运行产生 9 次正式调用，包括一次 summary 和工具后的后续调用。逐次核对：原图 bytes/MIME/次数不变；final-wire 字节数与 quote 一致；实际 SDK payload 和实际 HTTP body 的 context-bearing 字段与冻结 materialization 相同；同一 epoch 的 root/tools 及消息前缀连续。保留完整输入与规范化实际回复，不仅检查 HTTP 状态码。

基线证据（最终修复后需重新验收相关路径）：

| 目录 | 运行环境 | 结果 |
| --- | --- | --- |
| `scratch/k4-acceptance/luna-source-01/` | 源码 | 9 次调用及图片/前缀/恢复检查通过，但脚本末尾误用小写终止枚举，运行记录保留为失败 |
| `scratch/k4-acceptance/responses-source-01/` | 源码，修正脚本枚举后 | 通过 |
| `scratch/k4-acceptance/luna-wheel-01/` | 仓库外新 uv 环境、安装 wheel | 通过 |
| `scratch/k4-acceptance/responses-wheel-01/` | 仓库外新 uv 环境、安装 wheel | 通过 |

wheel 通过 `uv build --wheel` 构建，使用 `uv.lock` 导出的 runtime constraints 安装。运行目录只复制三份验收 harness，不复制源码或测试；清除 `PYTHONPATH`，确认实际包来自独立环境的 `site-packages/pulsara_agent/`。临时 PostgreSQL 数据库使用每次唯一名称，clean-v0 成功后运行并清理，不重置保存根库。

修复后的安装包记录：`wheel-final-build/`、`wheel-final-build.log`、`wheel-final-install.log`。`luna-wheel-final/report.json` 九次正式调用已通过。`responses-wheel-final/` 与 `responses-wheel-final-02/` 都在第二次纯图请求收到 HTTP 200 后的 SSE error/eof，规范化终止为 `PROVIDER_ERROR / unknown_provider_error`；失败记录保留，未修改 adapter 或删除图片重试。两次实际 HTTP 输入均与冻结 materialization 相等，图片没有省略；错误原因尚未确定。

用户授权新增的 DeepSeek 使用 `https://api.deepseek.com` 与 `deepseek-flash`。最小流式文本/图片实验均 completed，图片回答 `ORBIT`，证据 `deepseek-responses-minimal/`。随后同一修复后 wheel 的 `deepseek-responses-wheel-final/report.json` 完成九次正式 Kernel 调用并通过全部检查。最终 Chat/Responses 两组共 **18 次 COMPLETED**，每组跨请求共 **29 个 image occurrence**；两组均验证实际 HTTP 等式和工具/summary/cold/fork 链路。

## 最终矩阵与结论

已完成的独立检查：七个 `*architecture.py` 文件 **51 passed / 14.58 s**，日志 `scratch/k4-acceptance/architecture.log`；`uv lock --check` 通过。I43 双 API 生命周期及相关确认回归 **10 passed / 32 deselected / 5.15 s**，日志 `scratch/k4-acceptance/lifecycle-confirmation-final.log`。两次 pytest 均正常退出 0。

修复后全量第一轮为 **1969 passed / 6 failed / 19 warnings / 438.22 s**，日志 `scratch/k4-acceptance/full-non-live-final.log`。这六项已在最终整轮闭合：四项 Hook fixture 改为逐阶段精确检查 reservation 释放；一项 30 s writer lease 过期的原用例隔离复跑与最终整轮均通过，未放宽生产 lease；另一项为下述测试 node ID 问题。其中 I13 最初通过参数化新增图片，但改掉原 pytest node ID；已保留原文本节点、另增图片用例，共用实际 MCP 场景，未修改 oracle。两例与原 node oracle **3 passed / 76 deselected / 7.12 s**，日志 `scratch/k4-acceptance/mcp-image-reconnect-oracle-final.log`。

I36 增补真实 runner/PostgreSQL 的 Chat/Responses × 可接纳/完整 D2 超界组合，**4 passed / 5.79 s**，日志 `scratch/k4-acceptance/i36-large-image-compaction-final.log`。实际 summary safe-prefix 从 token 超界的最长候选截短；大于 2 MiB 的 mandatory 图片在完整成本允许时完成 adoption 并进入下一次调用，超界时没有 snapshot、adoption、图片 ref 或第三次 open。

最终命令 `.venv/bin/python -m pytest -q -m 'not retrieval_live'` 为 **1979 passed / 19 warnings / 416.93 s**，进程正常退出 0，日志 `scratch/k4-acceptance/full-non-live-final-rerun.log`。19 条 warning 均为 aiohttp 的 `shutdown_timeout` 弃用提示。`src`、`tests` 与 dogfood 的 Ruff、`git diff --check`、`uv lock --check` 均通过。没有新增 skip/xfail、放宽原 oracle 或改变 D1/D2。

主 agent 已交叉复核六个生产文件、fixture 迁移及 I36 真实组合；coding subagent 独立核对 lifecycle/dogfood。在上述整轮验收时，六个生产改动文件与最终独立环境安装的 wheel 内容逐个相等，没有记录新的文件 hash。K4 未增加 durable relation/event/job、provider 名称分支、图片丢弃重试或资源常数。

## 验收后的最终交叉审阅

2026-09-15，按用户要求，主 agent 与既有 GPT-5.6-sol/max coding subagent 再次独立对照 AGENTS.md、冻结的 D1–D4 和生产调用链，覆盖内容验证/存储/确认、reader/adapter/计量、prospective ROOT/Hook、Plan、压缩采用及取消释放。双方未发现其他可行动的文档偏差、生产旁路、无依据的资源上限或持久机制。已结束会话仅最初输入含图的疑点也从调用链证伪：prospective read 排除的是本次尚未发布的新 ROOT entry，旧初始图片仍属于 effective history。

本轮唯一代码删减位于 `provider_dispatch.py`：外层已经保证 `prospective_root_candidate is not None`，删除内层不可达的重复 `None` 检查，保留 borrow 与 `PreparedKernelModelCall` 检查。删减后的现有 prospective ROOT、自动压缩、direct admission、SessionStart/Hook 清理聚焦回归 **8 passed / 2.69 s**；Ruff 和 `git diff --check` 通过，未新增测试或修改断言。

主 agent 再次与已验收 wheel 逐文件比较，确认五个生产文件仍相同，`provider_dispatch.py` 只相差上述无行为变化的条件删减。本轮未重跑完整 pytest 或真实 provider；前述 1979 项及 18 次真实调用对应删减前的验收版本，不声称当前源码与该 wheel 逐字相同。

## I01–I44 证据矩阵

下列测试名均在当前仓库存在，最终整轮覆盖全部自动化用例；I17/I18 的真实请求与安装环境证据另列。


| 项 | 自动化证据与核验结论 |
| --- | --- |
| I01 | `test_llm_model_catalog.py::{test_input_modalities_preserve_known_unknown_and_missing_facts,test_invalid_input_modality_shape_remains_unknown_with_local_diagnostic}`：缺失/非法/未知逐行隔离，catalog 不增加 output authority。 |
| I02 | `test_llm_model_catalog.py::test_attachment_does_not_grant_or_remove_image_input_authority`：attachment 与 input modalities 正交。 |
| I03 | `test_llm_model_target.py::{test_frozen_target_input_modalities_gate_image_shape_before_provider_open,test_unknown_model_and_provider_native_wire_are_not_inferred,test_internal_empty_user_text_keeps_its_existing_closed_shape}`：只对实际图片做已知 text-only 拒绝；unknown 与纯文本保持原路径。 |
| I04 | `test_llm_model_target.py::{test_unregistered_models_dev_openai_compatible_route_uses_generic_wire_adapter,test_deepseek_uses_existing_generic_chat_and_responses_request_builders,test_exact_wire_lowering_belongs_to_generic_wire_api_adapter}`：按 wire API 选择两套通用 adapter，无 provider 名单分支。 |
| I05 | `test_kernel_image_input_k1.py::{test_prompt_content_is_one_ordered_typed_ingress_vocabulary,test_frozen_image_shape_and_user_role_are_closed}` 与 K3 mixed/pure wire goldens：顺序、重复、纯图和 role/part 闭合。 |
| I06 | `test_kernel_image_input_k3.py::test_pure_image_is_one_formal_user_wire_item_and_pure_text_is_unchanged`、`test_stage2_direct_model.py` exact materialization/prefix tests、`test_kernel_image_input_k2.py::test_snapshot_display_and_root_advisory_escape_text_and_mark_images_in_order`：纯文本 wire、replay/prefix 与新 carrier 展示均有 golden。 |
| I07 | `test_kernel_image_input_k3.py::test_chat_and_responses_mixed_image_wire_goldens_preserve_occurrences`：真实有效 PNG，Chat/Responses 的 URL、type、MIME、detail 与 occurrence 精确。 |
| I08 | `test_kernel_image_input_k2_postgres.py::test_direct_image_owner_confirms_and_reader_lowers_exact_typed_content` 联合 K3 wire golden：canonical publication→hydration→compiler→wire，来源与 bytes 不丢。 |
| I09 | `test_kernel_image_input_k2_postgres.py` 全文件、`test_stage5_clean_migration.py`：引用事务、完整性、GC、clean-v0/grant 走真实 PostgreSQL。 |
| I10 | `test_stage2_direct_model.py::test_final_wire_measurement_is_the_exact_adapter_payload_projection`、K3 traversal/golden；主 agent dogfood 另对 actual HTTP body 做等式。 |
| I11 | `test_kernel_image_input_k1.py::{test_v2_semantic_estimator_charges_each_image_occurrence,test_d1_visual_token_formula_is_frozen}`、K3 `test_v2_final_wire_quote_elides_only_formal_payload_and_adds_d1` / `test_v2_estimator_counts_repeated_images_and_does_not_scan_text`。 |
| I12 | K3 v2 quote/traversal/fact tests、`test_stage2_direct_model.py` final-wire/replay-prefix tests、`test_round5a2_durable_provider_replay.py` Chat/Responses replay tests：semantic/final wire/replacement 边界两侧闭合。 |
| I13 | 原 `test_round6_postgres_runner_commits_attempt_before_real_mcp_effect` 保持 node/纯文本场景；新增 `test_k4_images_preserve_prefix_across_real_mcp_effect_and_reconnect`：Text/A/Text/A，经真实 MCP effect 与 supervisor reconnect，三次输入图片为 2/2/4，已安装前缀精确相等。3 passed，日志 `mcp-image-reconnect-oracle-final.log`。 |
| I14 | `test_stage2_conversation_runner.py` 的 Tier 1/2/3/cold/model-switch 组、`test_kernel_image_input_k2.py::test_snapshot_codec_hydrates_in_owner_order_and_lowers_typed_content`、`test_conversation_fork.py` 图片 fork 两项及 ROOT advisory 测试覆盖来源矩阵。 |
| I15 | `test_kernel_image_input_k2.py` worker cancel/close/communication/start/reap 组、`test_stage2_conversation_runner.py::{test_k3_text_only_tier_three_projects_images_when_source_summary_fails,test_k3_soft_trigger_recovery_failure_closes_unpublished_prepared_input}` 及 provider termination 全量：无 adapter 图片重试，A typed 失败只进原 Tier 3。 |
| I16 | 全量 permission/Hook/MCP/plan/effect tests；重点 `test_round9_2_hook_integration_postgres.py`、`test_round6_mcp_production.py`、`test_round4_plan_*` 与 `test_stage2_conversation_runner.py::test_k3_output_resource_gate_precedes_assistant_and_tool_effects`。 |
| I17 | 正式 `tools/run_kernel_image_input_dogfood.py`：`luna-wheel-final/report.json`（Chat）与 `deepseek-responses-wheel-final/report.json`（Responses）各九次调用通过；记录完整原图/输入/回复、实际 HTTP 与 frozen materialization 等式。bobaigpt 两次 SSE error 单独保留，不计通过。 |
| I18 | 根 `.venv` 全量 non-live（含 PostgreSQL）1979 passed、正常退出 0，日志 `full-non-live-final-rerun.log`；实际代码来自仓库外 wheel 安装的 site-packages，两套图片链路各九次调用通过。Ruff、diff、依赖锁与 architecture 检查通过。 |
| I19 | `test_stage2_conversation_runner.py` PRE_FULL drift、无 executable prefix、disabled compaction、Tier handover 组与 `test_round5b_long_horizon_context_compaction.py` transition exact-join：dry successor 先验、失败不推进、idle selection 不采用。 |
| I20 | `test_stage2_kernel_host_dogfood.py::test_k2_root_install_prepares_ordered_image_gap_without_a_child`、K2 ROOT advisory/display、K2 direct Host test：无 child 的 ROOT 图片与 NONE/LAST_N/失败前投影。 |
| I21 | `test_stage2_kernel_host_dogfood.py` direct/steer image tests、`test_round9_2_hook_integration_postgres.py::test_k2_host_queued_image_hook_is_empty_once_and_conflict_is_exact`、K2 complete identity/Hook projection：direct/queued/steer 纯图混合、空文本 Hook 与次数。 |
| I22 | K2 PostgreSQL direct/full/ref-integrity tests、Host queued image conflict、`test_round5_long_horizon_postgres.py` FULL/NONE/CONFLICT 与 lost-ack tests：完整内容 in-flight/落库 exact join，确认不重跑 writer/Hook。 |
| I23 | K1 multipart quote/limit、K2 repeated ref metadata/hydration、`test_stage2_conversation_runner.py::test_k3_multipart_input_keeps_exact_canonical_service_headroom_before_open`：逐 occurrence 的 M/C/L/W/D1 与 batch gate。 |
| I24 | `test_stage2_conversation_runner.py::{test_k3_direct_input_failure_rejects_before_user_acceptance_and_provider_open,test_k3_soft_trigger_recovery_failure_closes_unpublished_prepared_input}`、`test_stage2_kernel_host_dogfood.py::test_k3_queued_root_resource_failure_rejects_before_consumption`、active steer/compaction tests：writer 前 prospective gate、可回收压缩、不可回收不消费。 |
| I25 | `test_kernel_image_input_k2.py` Pillow 格式/MIME/损坏/APNG/WebP 多帧/像素/PNG metadata 与 worker 生命周期全组；Host public error 测试证明 Hook/command 前返回具体原因。 |
| I26 | K2 snapshot codec、K2 audit carrier shape/occurrence tests、`test_round5b_long_horizon_context_compaction.py::{test_retained_historical_requests_roundtrip_is_ordered_and_has_no_legacy_parser,test_round5b_repeated_compaction_carries_runtime_owned_active_request}`：typed active/historical 多次 round-trip。 |
| I27 | K3 post-response upper/gate tests（tool escaping 两种 wire、root completion batch、Hook/clock/fresh Plan）、runner output-before-effects/control-feedback tests；65 个工具通过 65 次合法 batch 在同 turn 完成，证明无 lifetime cap。 |
| I28 | K1 canonical interleaving/duplicate refs、K2 PG direct/queue/fork/scope-misbinding tests：Text/A/Text/A occurrence 保留，owner ordinal 可重排但命令正文不变，反向 mismatch 失败。 |
| I29 | K2 PG `test_queue_redirect_and_steer_copy_independent_ordered_refs` / rollback、`test_pr04_prompt_queue_actions.py::test_redirect_reuses_large_blob_without_materializing_another_copy`、conversation fork 图片 tests：三 owner 本地 refs 与 caller transaction。 |
| I30 | K2 PG `test_image_ref_fk_gc_commit_orders_cascade_scope_and_runtime_grants`、`test_image_ref_gc_and_old_blob_attach_overlap_in_both_commit_orders`：真实双连接两种 overlap/旧 blob/rollback/cascade，helper 不暗开 connection。 |
| I31 | K2 PG `test_full_confirmation_reads_body_blob_but_not_image_payload`、scope/extra ref/损坏 hydration tests与 canonical reader integrity tests：RR 元数据确认不读图片 payload，正文实际 bytes 校验，I/O 错误不降格。 |
| I32 | `test_kernel_image_input_k2_audit.py` human origin shape tests、K2 snapshot codec、round5b retained history/fork tests、compiler PLAN/terminal/tool-result tests：kind/origin 联合与 placement 闭合。 |
| I33 | K3 ordinary suffix/retry tests、`test_round5b_long_horizon_context_compaction.py::test_round5b_summary_prefix_enumerates_every_finite_safe_boundary`、runner shrink-search/reclaim tests：固定 cut 下只移除最老 recent，资源失败集合封闭。 |
| I34 | runner Tier 2 older-history/recent-image、Tier 3 source failure/visual preservation 和 exact-three-tier tests；K3 window predicate/retry tests：无逐条滤图、补位或 mandatory 清空。 |
| I35 | K2 unified snapshot/display/ROOT projection escaping golden、K3 Chat/Responses wire、compiler working-set/placement tests：标签/JSON/引用可逆，P 是普通 Text，历史不触发新 human。 |
| I36 | 新增真实 PostgreSQL runner 组合 `test_stage2_conversation_runner.py::test_k4_compaction_prefix_search_keeps_large_image_mandatory_suffix`：Chat/Responses 各自以两轮 native replay 令 summary 最长 safe prefix 超出 token/final-wire budget，选中不含未发布第三条 ROOT 的较短 prefix；1024² PNG（>2 MiB）作为 mandatory suffix 通过真实 dry successor/adoption，下一 provider input 仍恰好 1 occurrence；2047² PNG 的同路径由默认完整 D2 C charge 在 writer 前以 `SOURCE_PHYSICAL_BOUND_EXCEEDED` 失败，零 snapshot/adoption/ref/第三次 open。4 passed，日志 `i36-large-image-compaction-final.log`。纯契约 `test_large_image_tail_uses_descriptor_budget_then_full_d2_charge` 另证明 descriptor 与完整 occurrence charge 分属两层；原 PLAN/terminal wrapper tests 保留。 |
| I37 | K2真实 Pillow 尺寸冻结与 snapshot hydrate、K3 wire traversal drift、actual materialization tests；主 agent dogfood 观察 SDK 与最终 HTTP bytes。 |
| I38 | runner Tier 3/P 与 final-wire shrink tests、K2 schema/grant/cascade PG tests：按实际 quote 缩 suffix，unknown 不触发 P，refs 无 UPDATE/DELETE runtime grant。 |
| I39 | K2 ROOT advisory/display、Host no-child ROOT image、round10 parent-context/LAST_N/NONE 与 compaction successor tests：snapshot 图片进 ROOT 主调用，nested carrier 不制造 public parent unit。 |
| I40 | runner `test_k3_text_only_tier_two_keeps_text_recent_when_older_history_has_image`、Tier 2 selected-image test、K3 active-exclusion/window predicates：有效历史任一图触发 Tier 2，只有所选窗口含图才 recent=0。 |
| I41 | runner cold Tier 3、source-summary-failure Tier 3、visual Tier 3 tests和 K3 P idempotence：统一 P 保留 Text/顺序/occurrence/kind-origin，无新 schema/状态。 |
| I42 | runner Tier 3 tests的 summary 与最终 mandatory wire断言、K3 P/quote tests：active exact、recent=0 不遮盖 mandatory，真实 B wire无历史 Image且 source proof/计量一致。 |
| I43 | `test_kernel_image_input_k4_lifecycle.py::test_omitted_images_do_not_return_after_cold_compaction_and_two_forks`（Chat/Responses）：P→cold→两次 compact→视觉→fork/refork，原正文/refs/FULL 保留且图片不复活。日志 `lifecycle-confirmation-final.log`。 |
| I44 | runner cold/source-failure/disabled-compaction/direct-failure tests、Hook block/cancel tests：source capture 在 P 前完成，B candidate 仍走完整 gate；损坏、取消、Hook block、planning 未完成与开关关闭不被省略绕过。 |
