# 压缩配置与请求开始：验收报告

日期：2026-10-30。状态：**实施与本规格要求的验收完成；ROOT 已完成实现与修复的 diff 复审。按实际运行范围记录结果，不宣称全仓执行通过。**

依据：[冻结实施规格](PULSARA_COMPACTION_MODEL_CONFIGURATION_AND_MANUAL_IDLE_ONLY_IMPLEMENTATION_SPEC.zh.md) §9。

## 环境与隔离

- 使用仓库 `.venv/bin/python`。每模块 PostgreSQL fixture 创建含 PID 与 UUID 的独立数据库；语料敏感测试使用独立 fixture，不共用待测 canonical 状态。
- 保存配置通过 `LocalSettingsStore`／`require_pulsara_home()` 读取，effective home 是 `/Users/plumliu/.pulsara`。保存设置未修改，模型凭据未输出或导出。
- 保存的 DeepSeek `deepseek-flash` 有 Chat Completions 与 Responses 两个真实连接。两者当前 catalog 的 effort 集合均为 `low/high/max`，本次只测试用户指定的 `high/max`，两档 binding 校验合法。
- 保存设置没有 PostgreSQL 字段。经授权，使用现有 test harness 的本地目标验证后，在内存中注入 UUID 临时数据库。验证的 admin DSN 为 `postgresql://plumliu@localhost:5432/pulsara`，runtime DSN 为 `postgresql://pulsara:pulsara@localhost:5432/pulsara`；物理连接均为 `::1:5432`、数据库 `pulsara`，用户分别为 `plumliu`／`pulsara`。不修改这两个根数据库的业务内容。
- 真实实验脚本：[run_compaction_request_start_dogfood.py](tests/dogfood/run_compaction_request_start_dogfood.py)。记录真实 adapter payload、summary／ordinary 输出、terminal 和 usage，并精确清除保存的凭据值。每次实验使用独立临时 home、workspace 和迁移数据库，结束后删除该数据库。

## 确定性回归范围与结果

以下分别记录各次实际运行，不相加重复覆盖的数量，不宣称全仓测试执行通过。

| 范围／命令 | 实际结果 | 证据 |
|---|---|---|
| 32 模块联合回归，完整模块清单见 `joint_executed_modules.json`；命令为 `.venv/bin/python -m pytest <清单内32模块> -q --tb=short --junitxml=dogfood_evidence/compaction-request-start-20261030/joint_regression.xml` | 330 passed／4 个旧 Hook API 测试失败，309.05s；旧 Hook 已由下面当前合同的整模块补测闭合 | [模块清单](dogfood_evidence/compaction-request-start-20261030/joint_executed_modules.json)、[JUnit](dogfood_evidence/compaction-request-start-20261030/joint_regression.xml)；四项失败均为旧 Hook 模块退役 API；对应当前合同的整模块补测已通过 |
| `.venv/bin/python -m pytest tests/test_session_workspace_recovery.py tests/test_compaction_handle_settlement.py tests/test_compaction_host_lifecycle.py -q --tb=short --junitxml=dogfood_evidence/compaction-request-start-20261030/supplement_regression.xml` | 31 passed in 30.79s | [JUnit](dogfood_evidence/compaction-request-start-20261030/supplement_regression.xml)；workspace／handle4／Hook question 后第二 steer／manual 与 stop lifecycle |
| `.venv/bin/python -m pytest tests/test_compaction_successor_control_races.py -q --tb=short --junitxml=dogfood_evidence/compaction-request-start-20261030/successor_races_regression.xml` | 8 passed in 14.77s | [JUnit](dogfood_evidence/compaction-request-start-20261030/successor_races_regression.xml)；terminal FULL→group fresh／ACK、原 cause／raw cancel／Host close、零重驱动与占用释放 |
| `.venv/bin/python -m pytest tests/test_compaction_starting_stop_control.py -q --disable-warnings --maxfail=1 --junitxml=dogfood_evidence/compaction-request-start-20261030/starting_stop_regression.xml` | 1 passed in 3.04s | [JUnit](dogfood_evidence/compaction-request-start-20261030/starting_stop_regression.xml)；读取终态失败时 control 为 FAILED／FINISHED，原 turn USER_STOPPED，零伪结果与 busy |
| `.venv/bin/python -m pytest tests/test_provider_usage_postgres.py -q --tb=short --junitxml=dogfood_evidence/compaction-request-start-20261030/provider_usage_regression.xml` | 6 passed in 5.79s | [JUnit](dogfood_evidence/compaction-request-start-20261030/provider_usage_regression.xml)；idle manual 新接口、usage anchor 和 current SUBAGENT scope |
| `tests/test_kernel_image_input_k3.py`，由其 owner 执行完整模块 | 82 passed in 40.29s | 退役 tier selector 清理，保留图片／owner／目标投影边界 |
| `tests/test_kernel_image_input_k4_lifecycle.py`，由其 owner 执行完整模块 | 2 passed in 5.00s | 双协议 idle manual 后新 turn 采用 revision>0／SNAPSHOT；原 terminal cut／refs／content 保留，后续 wire 不含历史图片 |
| `.venv/bin/python -m pytest tests/test_round9_2_hook_integration_postgres.py tests/test_round9_2_hook_subsystem.py::test_round9_2_post_adoption_status_revalidation_is_bounded_and_control_linearized -q --disable-warnings --maxfail=1`，由该 owner 执行 | 12 passed in 31.23s；subsystem 最后 trigger 语义更新后单项另 1 passed in 0.41s | 当前 integration11 项＋subsystem1；替换退役 SubagentStart/Stop、active manual 和旧 alias，保留共同 Hook、Work final、Pre/Post/compact SessionStart 与实际 wire prefix |
| `.venv/bin/python -m pytest tests/test_scheduled_tasks_host.py -q --tb=short`，由该 owner 执行 | 11 passed in 16.18s | 真实 auto summary＋非法工具输出 repair 两次、revision1、实际 Schedule 暂停，fork／父会话删除后 suffix 原文与非 human 来源归因恰一次 |
| Scheduled typed byte-charge 的 source-boundary focused，由 compaction owner 执行 | 8 passed | 补齐 `SCHEDULED_TASK` 的既有计量归类，精确 lineage 正反例与预算 guard 保留 |
| `tests/test_stage2_conversation_runner.py`，由其 owner 执行最终完整模块 | 115 passed | 包括短历史实际 summary 的增长可容纳／真实硬预算失败，以及多模型目标投影、首次 group、当前 turn adoption |
| `tests/test_compaction_first_call_source_boundary.py`／历史 interruption／provider safe point／资源屏障，由 owner 执行 focused 组合 | 102 passed | 整组 source ceiling、snapshot-only 前驱、Interrupted 来源保留、freeze 物理取消 drain、Installed／Empty 权限分型 |
| 配置／协议／前端由对应 owner 执行 | 配置与协议 focused 84 passed；模型 target/connection 91 passed；allowance PostgreSQL 9 passed；frontend adapter 111 passed，Workbench 新项 1 passed | typed effort／toggle／budget、actual fake-wire summary/repair、snapshot target、manual 与 exact stop 投影；tsc 与 desktop build 通过 |

全仓 collect-only 已实跑 **4,661 tests collected in 2.00s**，只证明可收集，不等于执行全部测试。退役 root candidate/admission／prospective／source-summary 接口检索已清理；已审阅全部 51 个 compaction/manual 调用点。最后覆盖全部 **99 个已改／新增 Python 文件的 Ruff 通过**，`git diff --check` 通过；原始结果见 [final_checks.json](dogfood_evidence/compaction-request-start-20261030/final_checks.json)。无剩余阻断，不再扩大检查范围。

## 实际缺陷的修复证据

本轮验证不只清理旧测试调用。真实 owner 屏障发现并修复了首次压缩后遗漏原始 human memory-disable／recall 触发、采用后 suffix 的 ACTIVE_SKILL 来源契约、FULL 已提交后 group 读取失败遗留占用、重复取消时返回 handle／Hook reservation 的收束，以及停止确认读取失败后 control 永久 PENDING 等问题。对应测试继续核对真实 turn／queue／revision、实际首 ordinary wire、物理资源关闭和首个 cancellation cause，没有用 skip、宽松 guard 或伪成功绕过。

最后的 Scheduled 场景还暴露了生产计量缺口：`SCHEDULED_TASK` 输入未进入其既有 typed byte-charge 归类，导致实际自动摘要采用后发生 POST_FULL lineage 精确匹配失败。修复只补齐该输入来源的原有计量合同，保留 exact-join 与预算 guard；source-boundary 正反例由 owner 实跑 8 项通过，Scheduled 完整模块 **11 passed in 16.18s**，source-boundary focused **8 passed**；故障已闭合，结果见回归表。

## 新真实 owner 屏障集成

文件：[test_compaction_request_start_integration.py](tests/test_compaction_request_start_integration.py)。模型 transport 可控，Host、writer、queue、canonical reader、压缩采用、输入 safe point 和数据库均使用真实 owner。

| 场景 | 当前结果 | 证据要求 |
|---|---|---|
| Installed ROOT direct 首次摘要 | 已通过 | summary 屏障内唯一 RUNNING、原 opening entry、revision0、query／live target 相同，普通 open 为零；三来源真实混排并进入首 ordinary wire |
| Installed ROOT queued 首次摘要 | 已通过 | 上述事实加 queue CONSUMED→instruction entry、request running；原 NEW_TURN 转 steer 后不再执行 |
| fresh Host／Empty Responses 加普通 steer | 已通过 | 未安装前驱时真实采用、后续合法 cold seed、首 ordinary 含原文和补充；Responses fixture 保留原生 replay 合同 |
| fresh Host／Empty 原始 human 禁用 memory、无 steer | 已通过 | 首 ordinary 的 `memory_context.memory_use_policy` 为 ALL_DISABLED_BY_USER，无 recall／hint 投影 |
| human 禁用 memory 后 Agent 试图恢复 | 已通过 | 来源不能升级人类授权或重新开启 memory，最终实际 policy／recall／hint 受原规则约束 |
| Subagent profile→instruction→material 整组首次摘要 | 已通过 | instruction 不是最早 entry；整组 source ceiling、Work running／current target、首 ordinary 原文＋材料＋补充 |
| adoption FULL 后重复 stop，有／无丢失 ACK（两项） | 已通过 | 实际提交后 ACK 屏障；revision1／snapshot／采用事件各一；原 turn USER_STOPPED，待 steer 全终结；零 ordinary、borrow 释放 |
| R1 真实摘要中停止，R2 再次摘要 | 已通过 | R1 不重放，R2 摘要及 ordinary 恰一次；R1 steer 不投后继；独立后台 Agent Work 继续 running |
| terminal monitor 新 turn 首次摘要 | 已通过 | 真实 Process 完成→TERMINAL_OBSERVATION＋RUNNING＋revision0；summary source 不含当前观察；补充进入首 ordinary |

上述十项均已纳入 32 模块联合回归并通过；另外已包含独立 Plan 两项（无／有 steer，实际 revision1 和完整批准 Plan wire）、suffix 三项、binding 六项及原始 recall exactly-once 两项。

投递的 exactly-once 检查按 canonical entry 与实际独立文本 part 身份判断。合法 memory recall 可以引用已完成 Work 的同文；不能把这种独立检索投影误判成重复消费或重复投递。

## §9 覆盖矩阵与剩余项

| 规格场景 | 证据落点 | 当前结论 |
|---|---|---|
| idle 短历史同模型仍摘要、无 turn／assistant | 新真实脚本 `chat_high_idle`；Host lifecycle tests | 真实手动矩阵已过；短历史 actual transport 两项亦随 main runner 全115项通过 |
| 手动 B／历史 A，直接目标摘要 | model configuration／runner focused；真实跨协议 case | typed 配置与目标投影已过；真实跨协议切换已过 |
| 空历史／已有队列／活动／Hook／其他 manual busy，拒绝不复活 | Host lifecycle／long-horizon runtime tests | 空历史、队列、活动、并发 manual、真实 Hook 准备屏障均已过 |
| manual 接纳后正常发送入队，成功／失败／停止释放 busy | Host lifecycle tests | 已过；真实成功后入队交付也通过 |
| manual 配置与设置／普通接纳竞争 | model configuration／Host lifecycle | 真实 pre-admission writer屏障捕获high后UI改low→原candidate仅写一次明确失败，零summary／commandrow／busy；postaccept冻结和queue先提交busy亦过 |
| 三入口非默认 reasoning、repair 保持完整选择 | 配置 payload tests；四 case 真实矩阵 | typed fake-wire 与真实三入口已过；真实未需 repair，repair 由确定性 wire 证明 |
| 活动修改 UI 配置／steer、不换当前配置 | 配置 tests／Host 新集成 | 真实 Host 配置捕获五项已过；实际 summary／ordinary payload 保持所属配置 |
| 多条请求各自配置及 opening entry | request execution／queue tests | 真实 Host 多 NEW_TURN 与多 Work 各自冻结 binding 已过 |
| ROOT／Subagent 首次摘要可接纳补充 | 新真实屏障集成 | ROOT／Subagent 已过 |
| 首摘要真实 RUNNING／完整组／revision0／CONSUMED／open0 | 新真实屏障集成；runner focused | ROOT／Subagent 已过 |
| profile 在 instruction 前／依赖材料在后 | source boundary tests／新 Subagent 集成 | source boundary 与端到端已过 |
| 未取得执行位置 Work、W1／W2 目标 | Agent runtime／Host lifecycle | 容量一份、W1 当前目标、W2 排队及依赖既有回归已过 |
| 普通发送与队列转换均可接纳、转换原子 | 新真实 ROOT 集成／queue actions | ROOT 主路径与原 final／转换竞争已过 |
| R1、Q1／Q2 转 steer 顺序 | 新真实 ROOT 集成／Host lifecycle | Q2 首 ordinary 与 Q1 独立后续调度已过 |
| summary 中 steer 不重启摘要、进入首 ordinary | 新真实 ROOT 集成；四 case 真实矩阵 | 确定性与真实首次／turn 内／切换场景均已过 |
| Installed／Empty 的合法 continuation 权限 | provider focused／新真实集成 | normal steer 两类已过 |
| memory trigger／recall／hint 与采用后 suffix | 新 memory 集成／provider focused | 初始 human 禁用、human-disable 后 Agent 追加均已通过 |
| safe point 截面之后消息保留、不追逐 | provider／steer focused | 真实 postadopt 截面屏障已过：late 消息首 wire 不含、第二 wire 消费，prefix 连续 |
| suffix 超预算／不支持内容、真实最终测量 | final-input budget／provider focused | 真实采用后已选 suffix 的预算／图片能力 typed 失败已过；零 ordinary open、adoption 保留、目标 queue 终态 |
| 三来源混排与 final 竞争 | 新 ROOT 集成／queue actions／Host lifecycle | 三来源首 wire 与原 final／转换竞争已过 |
| stop 与接纳／准备竞争、单终态、不改投 | Host lifecycle／admission focused | 首准备、消息目标、queue 后项隔离已过 |
| 启动前无 turn／启动后失败保留原 turn 和 queue | admission／runner／Host budget tests | 已过 |
| start ACK unknown 后 stop／binding 推进 | canonical PostgreSQL／admission tests | canonical／admission 真实专项与联合回归均已过 |
| 确定拒绝 NONE／cancel NONE 不重发 | admission focused | admission 11 项已过；包含 raw task.cancel＋NONE 不重发 |
| ACK unknown 后 writer replacement | provider／admission focused | provider 明确失权退出与 canonical／admission takeover 专项已过 |
| 重复 stop／close 于确认／TODO／adopt unknown，资源退役 | provider／Host lifecycle／TODO tests | start ACK／TODO／资源 drain 与真实 adoption FULL／丢 ACK 后重复 stop 均已过 |
| R1 stop、R2 正常启动／可再摘要 | Host lifecycle／Agent session | 真实 R1 摘要停止后 R2 再摘要及 ordinary 恰一次均已过 |
| 容量一份 Work／崩溃 takeover 不重放，W2 恰一次 | Agent session／Host lifecycle | 物理容量恰释放与 takeover 不重放均已过 |
| 自动软失败可继续／硬失败终结／cancel 不吞 | runner focused | runner 完整115项与联合边界回归已过 |
| summary/adoption 失败及 unknown 确认、不回滚 | coordinator／provider focused | 真实 FULL 后丢 ACK／重复 stop 保留 snapshot/revision/event、零发送已过；原 summary/repair source 保留回归已过 |
| 命令重试／close／Host replacement／查询 | configuration／admission／Host lifecycle | configuration／admission／Host lifecycle 真实与资源专项及联合回归均已过 |
| Cell／Process／Monitor／Work／PMCA／TODO 保持 | 既有长程与 Agent／TODO 回归 | 真实 Chat／Responses mid-turn 压缩组合两项已过：同对象仍运行、同 TODO owner／完整16项、PMCA≤3200而完整交接>3200 |
| Hook／Plan／TODO／memory／terminal 独立 caller | Plan 25 项／Hook identity／TODO／新 memory | Plan needs-summary两项、terminal needs-summary一项、当前 Question已解后第二steer均已过；当前 Hook integration11项与subsystem补测已通过；退役 API 不保留 |
| SYSTEM/tools 连续、messages suffix、quote/permit、资源 | prefix／provider／新真实 wire | 确定性 normal 路径与真实 summary→ordinary 已过 |

## 真实 DeepSeek 矩阵

已完成的最小矩阵如下。所有真实调用均为保存的 DeepSeek `deepseek-flash`，不临时替换服务或模型。

| case | 目标协议／effort | 路径 |
|---|---|---|
| `chat_high_idle` **通过** | Chat Completions／high | idle manual 实际摘要；摘要进行中接纳普通请求；后续 ordinary |
| `responses_high_first` **通过** | Responses／high | fresh Host／Empty 首调用前摘要；摘要中转 steer；首 ordinary |
| `chat_max_active` **通过** | Chat Completions／max | 当前 turn 工具后自动摘要；摘要中转 steer；后续 ordinary |
| `responses_max_switch` **通过** | Responses／max | 保存 Chat/high 连接先真实调用，再切保存 Responses/max 目标并摘要；首 ordinary |

最后一项是同一个模型的**跨协议 target／connection 切换**。不同 model 的 A→B 逻辑以确定性测试另行覆盖，不能将同模型跨协议实验写成两个不同模型。

完成条件：每个协议×effort 至少有一份真实成功 summary 和其后 ordinary 的实际 adapter payload、provider terminal、输出与 usage；补充 marker 必须出现在该摘要后的第一次 ordinary input。脚本保存完整输入输出，失败也保存报告，不以队列行存在替代真实 wire 证据。

实际参数：两个 Chat 场景的 summary／ordinary payload 分别为 `reasoning_effort=high`、`reasoning_effort=max`；两个 Responses 场景分别为 `reasoning={effort: high, summary: auto}`、`reasoning={effort: max, summary: auto}`。以下各调用均返回 `COMPLETED`、usage `reported`。

| case | summary input／output／reasoning tokens | 摘要后首次 ordinary input／output／reasoning tokens |
|---|---|---|
| Chat high idle | 42,535／623／168 | 29,891／39／27 |
| Responses high first | 38,411／497／101 | 29,890／254／177 |
| Chat max active | 42,137／609／133 | 30,918／38／0 |
| Responses max switch | 38,625／636／195 | 29,983／277／197 |

原始证据保存在忽略提交的本地证据目录，均已通过保存凭据 scrubber 再检查；两次 UUID 数据库均已删除：

- [完整首轮报告](dogfood_evidence/compaction-request-start-20261030/pulsara-compaction-request-start-20261030.json)：三个 case 通过。Chat/max 首次 ordinary 与工具实际成功，但按预响应 heuristic 设定的实验阈值高于 provider usage-anchor 校准后的输入，未触发摘要，因此该 case 及报告总状态明确保留 `failed`。
- [Chat/max 定向重跑报告](dogfood_evidence/compaction-request-start-20261030/pulsara-compaction-request-start-chat-max-20261030.json)：按首 ordinary 真实 terminal 的 reported input tokens 设置实验软阈值，在工具后共同边界真实触发摘要，summary 与后续 ordinary 均通过。没有修改首轮证据或生产预算语义。

原始输出位置亦保留 `/tmp/pulsara-compaction-request-start-20261030.json` 与 `/tmp/pulsara-compaction-request-start-chat-max-20261030.json`；最终本地证据以仓库内 `dogfood_evidence/compaction-request-start-20261030/` 的副本为准。该目录被 Git 忽略，不能假定会随提交分发；已另提供 [真实 provider 证据压缩包](dogfood_evidence/compaction-request-start-20261030/real-provider-evidence.zip) 供独立保存。

执行命令：

```sh
.venv/bin/python -m tests.dogfood.run_compaction_request_start_dogfood --output /tmp/pulsara-compaction-request-start-20261030.json
.venv/bin/python -m tests.dogfood.run_compaction_request_start_dogfood --case chat_max_active --output /tmp/pulsara-compaction-request-start-chat-max-20261030.json
```
