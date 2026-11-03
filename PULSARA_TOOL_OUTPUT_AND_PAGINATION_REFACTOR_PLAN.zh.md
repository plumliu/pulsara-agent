# Pulsara 工具分页与长结果：审阅及推荐算法

日期：2026-11-03。状态：只读审计建议，尚未实施，也不是已批准的实现规格。

本轮由 8 个 `gpt-6.1-sol / high` 代理分别审查全部 38 个 builtin，以及目录外的动态 MCP 工具；主代理交叉核对公共结果链路。未修改生产代码、设置或数据库，未调用真实 provider/MCP。除 `query_cells` 的纯内存生产函数复现外，没有运行 Host/provider 或整套回归测试。

后续由其中 5 个 sol / high 代理与主代理交叉讨论方案。当前推荐：删除 ARTIFACT 身份和 `read_artifact/export_artifact`，由 `read_file` 读取原结果正文、`write_file(source=原结果)` 创建真实文件。理由是操作概念更少、避免自动文件副本、保留明确读取与写入边界；不考虑兼容当前数据，也不以代码改动量为评判标准。完整算法见第 14 节。

## 1. 本次要解决的问题

- 源工具先分页，但一页又被通用层截成预览，模型必须先读 artifact 才能读完这一页。
- 页大小常按条数、字符数或内部 JSON 字节计算，没有计入最终工具消息的转义、元数据和续读字段。
- 出现 artifact 只表示保存了本次结果，不表示正文被省略，也不表示整个数据源已保存。
- 源下一批、旧结果剩余正文、后续新输出是不同操作，目前续读指引不够明确。
- 某些内容在进入 artifact 前已经被切掉；Code Mode 同样拿不到这些内容。
- 改动不能破坏文件编辑观察、已执行副作用、Code Mode 主动披露边界、权限、取消结算或 provider prefix 连续性。

## 2. 建议的共同边界

1. 源分页的正常页应完整交付。条数和字符参数表示请求上限；实际页面可以更小。复用现有完整工具消息计量，不能另造字符/token 换算公式。
2. 对记录页按完整记录选取可容纳的前缀；对文本页按明确的字符坐标切片。每次试算包括实际元数据、续读指引、JSON 转义和正常/晚到外壳。
3. 最小记录或一个字符放不下时，给明确资源边界或现有的单项展开入口；不能跳过记录，也不能成功返回不前进的续页。正常 EOF 与无进展失败分开。
4. 给模型可直接复制的完整下一步调用。继续源查询和读取保存正文明确区分；不新增万能 cursor、分页工具、持久注册表或快照任务。
5. 完整正文归原结果，不分配独立 ARTIFACT 身份，不保留专用 artifact 读取/导出工具。`read_file` 接受精确结果来源，`write_file` 接受完整结果作为内容来源；普通完整页不要求再读一遍保存正文。
6. 存储、展示和业务真值分开。完整显示一页不等于全源已穷尽；读完保存正文不等于源无下一页；省略回执不允许重新执行已经成功的操作。
7. Code Mode 继续处理其实际取得的 public value。它不能恢复上游丢弃的内容，不能突破既有桥接边界，也不能借分页调整扩大工具可见性。
8. 修改描述、schema 或 SYSTEM 仅在合法新 cold epoch 或明确采用的 compaction successor 生效；不在已安装 epoch 内替换前缀。

现有共同实现：[完整工具消息计量](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/primitives/tool_result_projection.py:253)、[保守计量入口](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/primitives/tool_result_projection.py:329)、[结果交付分类](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/primitives/tool_result_projection.py:104)、[归档与预览](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_artifacts.py:145)。固定单条逻辑消息边界与整次 provider 输入可用预算不是同一件事，两层都必须通过现有 owner。

## 3. 已确认的重点问题

| 问题 | 证据与准确范围 |
| --- | --- |
| 终端同次正文被决定两次 | terminal 先取 tail；通用层又从完整 candidate 重建 native 正文。Code Mode 取得原 public value，可能与 native 看到的正文不同。`max_output_chars=512` 因此不必然限制 native 为 512 字符。[输出选择](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/terminal_process/output.py:557)、[公共预览](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_artifacts.py:287)、[JS 交付](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_execution.py:498) |
| 搜索先丢信息，再归档 | 搜索完整收集、过滤、排序后切页；匹配行先 `[:500]`，且未标记该行被截断。artifact 无法补回尾部，命中还可能在展示片段以外。[搜索实现](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/filesystem.py:560) |
| Cell 页面计量错误已在内存中复现 | 生产 `_query_cells` 对 20,000 个反斜线的源码返回 19,693 字符；内部 JSON 为 39,671 bytes，通过当前检查；完整保守工具消息计量为 79,425，超过 40,000。未运行 Host/provider，不能声称其下游实际故障已端到端复现。[适配器](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:3072) |
| 有页内 fit，仍不保证最终完整交付 | 历史工具已有完整页面 fit 和真实 inline 标志，不会再套 artifact；但仍属 BEST_AVAILABLE，可被后续输入编译降级。文件、代理和调度等也需分别闭合最终交付要求。[历史返回](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:3307) |
| 列表携带完整大正文 | Work 列表携带 instruction/final/diagnostic；schedule 列表携带 prompt。应先精简列表，再对选定对象读取正文，不能只把现有大列表标为 FULL。[代理查询](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/subagents/collaboration_api.py:284)、[调度源页](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/_repository/scheduling.py:124) |
| 不能只改 descriptor 的 NEVER | `artifact_mode` 声明不直接决定 processor 是否归档；真实防递归/不套页依赖执行时的 `artifact_inline_result` 等路径。历史工具与 read/export 已有真实执行标志。[processor 条件](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_artifacts.py:155) |

## 4. 文件读取与搜索：3 个

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `read_file` | 读取 PATH 或本次已接受结果正文。按完整消息预算装页，长行在同一入口分片，完整 next 携带位置。PATH 可保留语义行范围，结果来源首次只需 target。 | PATH 精确 revision 与实际交付行的编辑观察；结果来源冻结且独立校验 scope，不授任何 PATH 编辑资格。 |
| `search_content` | 保留三种搜索模式；结果页自适应预算。content 模式返回围绕真实命中的 excerpt、文件和行位置，完整当前源经 read_file 取得。采用第 15 节的文件位置续查：已完成文件不再搜索内容，不保留跨调用 producer。 | pattern、root、glob 和目录/显式文件各自的 ignore/hidden/binary/symlink 语义；不承诺跨页快照。排序明确切到 rg 原生目录组件序，默认不算全局总数。 |
| `find_files` | 流式枚举、筛选、装完整 PATH 页；next 携带文件边界，不增加字符分页，不为精确总数收集全量。页满可保守返回 next，末尾允许一次空 EOF。 | 完整可复制路径、原 glob 语义及明确的 rg 原生顺序。单个 target 放不下时明确边界，不能拆路径或静默跳项。 |

保留有独立任务意义的源范围参数，不为工具表面整齐强行统一查询语言。源页完成交付与文件观察安装要一起核对；`edit_file` 依赖的是同 revision 的已观察行，不是“原文曾在内部生成”。

证据：[文件读取与观察](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/filesystem.py:334)、[搜索分页](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/filesystem.py:592)、[文件编辑观察测试](/Users/plumliu/Desktop/python_workspace/pulsara_agent/tests/test_content_revision_line_edit.py:235)。

## 5. 终端：3 个

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `exec_process` | 同次观察正文只生成一次；建议以 terminal 的 tail 为显示结果，再按最终消息预算收紧，公共层不得从 candidate 重新展开。真实省略时提供该次冻结正文入口，即使全文不足 8,000 bytes。 | command、cwd、tty、初始等待、PROCESS target、status/exit_code、物理执行与结算。启动不是分页。 |
| `query_processes` | 列表只保紧凑进程元数据，完整 command 放单项。模型元数据查询不应提供看似已读取输出的推进游标；日志继续由实际 poll/wait 取得。 | 查询不消费输出、不返回日志；不把目录查询当作一次输出观察。 |
| `manage_process` | poll/wait 保留新观察 cursor；write/submit 建议返回写入前后之间的 delta，避免重带全部旧日志；close_stdin/kill 建议仅返回真实控制状态，正文通过显式观察取得。 | wait 的现有等待语义、PTY echo 与完成的区别、kill 等待物理退出、cursor gap 与显示省略的区别。补读旧结果不能重做控制动作。 |

保留既有进程/Host 日志资源边界，不新增自动持久 `.log` 作业。quiet、主动日志重定向和 Code Mode 筛选可作为模型建议，不能替代意外大输出的兜底。日志已落普通文件时使用现有文件/终端能力；没有保存的内容必须诚实报告不可恢复。

证据：[terminal 参数](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/ports/terminal.py:13)、[冻结范围与 cursor](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/terminal_process/output.py:557)、[保留驱逐](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/terminal_process/manager.py:1411)。

## 6. 能力与 MCP：5 个 builtin，加动态工具

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `query_capabilities` | list 给完整下一次调用，保留实际预算降行；列表只放必要身份和说明，说明太长时优先 inspect。MCP_TOOL inspect 的 schema 始终整体可用。 | list/inspect 分支、完整 target/invocation、已有 FULL 交付与 schema 超界类型结果；普通 inspect 的长详情可用保存正文。 |
| `manage_capability` | 参数不加分页。状态、current、adoption、必要 guards/targets 作为完整控制结果，大诊断与审阅正文单独按需读。 | 准备/批准/执行/采用边界；采用失败不等于 mutation 未执行，不能为补回执重放。 |
| `reload_capabilities` | 保留 `{}`；长报告读同次已保存正文，不用再次 reload 取得所谓下一页。 | ROOT/bypass、只对未来能力生效、当前 epoch 不 rebase。 |
| `use_new_mcp_tool` | host 参数不增加分页；明确原 schema 的源分页与本次保存正文续读。给可复制的正确读取动作。 | 原远端 arguments、已核对 ref、executor/permit、已接受业务效果。 |
| `read_mcp_resource` | 不增加 offset；明确再次读取取得新远端内容，本次正文展开走统一保存结果入口。 | concrete/template 现有 target/uri 语义；不把 `resources/read` 当作正文分页协议。 |
| 动态 `mcp__…` 工具 | 与 META 使用同一结果完整性和续读说明；只允许远端原 schema 本来支持的分页。 | 冻结 schema、调用与权限绑定，不能擅改参数或重执行有副作用调用。 |

SDK 发现工具/资源时的协议 cursor 是另一层内部机制，不能当作模型 `query_capabilities.offset`。完整 callable schema 不能拆成几段让模型猜着调用。MCP 已有传输/lowering 边界继续生效，artifact 不能绕过被上游拒绝的响应。

证据：[能力页面 owner](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/capability/source_query.py:199)、[MCP_TOOL inspect](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:2918)、[MCP 执行](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/mcp/supervisor.py:686)。

## 7. 会话历史：3 个

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `search_sessions` | limit 是最多会话数；返回完整源 next 和命中正文的展开调用。普通成功页保证完整交付。 | 当前排序/筛选与授权，源动态变化不承诺全局冻结库存。 |
| `search_session_content` | 保留问答过程这一候选单位；摘录提供精确 read 调用，继续检索提供 source next；不让增加 limit 被误认为补全摘录。 | 现有相关性检索、已消费候选与进程内 cursor 生命周期；重启过期明确重查。 |
| `read_session_content` | 沿已有单 entry 内切片和历史 cursor 读取，不另套 artifact；补最终完整交付保障，返回可复制 next。历史工具结果可通过 typed renderer 减少 preview 与保存正文的重复展示。 | paired cut、方向、冷历史范围、占位图片原子性、源缺失事实和每页重新授权。 |

这组三工具当前已通过真实 inline 标志避免再生成 artifact；问题主要是内部 fit 后仍可在 provider 编译阶段降级。不能为统一结果读口直接放开当前历史：精确当前 tool-result、子代理权限与已披露 Cell 输出都需要另外明确的狭义合同。

调用者 ROOT-only 与目标是否为 ROOT 会话不是同一约束；当前目标授权与文案有需核对之处，不在本轮自行扩大或收紧权限。查询命中和精确读取对回复批注引用的呈现差异，也应在承诺精确原文前处理。

证据：[历史页面 fit](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/session_content.py:147)、[历史工具正文展开](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/session_content.py:699)、[已存在的 entry 续读](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/session_content.py:898)。

## 8. 代理与调度：7 个

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `run_agent` | 原参数和短接纳回执不加分页；提供返回 WORK 的完整查询/等待动作。 | 接纳不等于完成，不因补结果再次 run，不按显示预算缩减实际接纳集合。 |
| `query_agents` | Agent/Work 列表自适应预算；Work 列表去掉完整 instruction/final/diagnostic，保留身份、状态和单 WORK 展开入口；单 WORK 长正文用既有保存结果。 | ROOT/子代理可见性、按既有身份/序号遍历，不新增报告表或持久 cursor。 |
| `send_agent_message` | 保留短回执，不加分页、不回显消息正文。 | queued 只表示接纳到原活动 turn；不自动转投后继 Work，不为补回执重发。 |
| `wait_agent` | 保持状态/目标/来源的轻量结果；完成项给 WORK 读取动作；大量目标的当次结果需可恢复。 | 绑定开始等待时的 Work，不追随后继，不把再次 wait 当作读旧正文；不新增目标总数上限。 |
| `stop_agent` | 不再返回每个受影响 Work 的全部正文；返回身份/真实终态/查询动作。巨大取消集合仍以当次结算结果保留。 | 实际取消范围不受页大小影响，不把随后查询所得集合冒充当次取消集合，不重做 stop 来翻页。 |
| `query_schedules` | 模型列表去掉长 prompt，只列完整 target/revision/状态/时间规则及单项入口；按 provider 结果预算缩页，next 携带解析后的会话与筛选条件。 | 模型默认会话、用户级 HTTP 页面和后台调度的不同 owner；单项配置按 revision 解释。 |
| `manage_schedule` | 事务参数保留；create/update/switch 等缩为完整短回执，不重复长 prompt，正文用单项查询。 | expected_revision、switch 原子性、delete 已删除对象的真实回执；不能重执行取得丢失正文。 |

证据：[代理 list/detail 投影](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/subagents/collaboration_api.py:168)、[stop 结果](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/subagents/collaboration_api.py:390)、[调度源分页](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/_repository/scheduling.py:124)。

## 9. 记忆：4 个

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `search_memory` | 保留 top-k 召回，不改成库存翻页；对本次已选结果按预算显示身份和明确标注的正文摘录，完整当前正文用 get。仍放不下的已选结果应有本次保存输出入口，不能用减小 limit 重搜冒充续读。矛盾提示被筛选或裁减时不能暗示已返回全部相关关系。 | 排序/召回语义、实际模型可见 memory provenance，不新增全集 cursor。 |
| `get_memory` | 保留精确记忆正文入口；检查 direct relations 的固定截取及完整性表达。正文与关系列表分别处理，是否提供全部关系枚举另作产品决定。 | 正文既有大小约束、来源身份和权限；不能把返回的一部分关系说成全部。 |
| `remember` | 参数/写入语义保留；缩短回执中的 related 正文，完整保留 SAVED/ALREADY_PRESENT 等状态、target 和必要提示；成功回执保证完整交付。 | canonical 写入与真实 exposure，不能因回执被省略重写记忆；相关完整正文通过已有 get 取得。 |
| `mark_memory_relation` | 不加分页；保持小型完整关系写入回执，保护 status、relation_id、source、target、kind 与必要 advisory。 | 关系事务、既有身份与幂等语义，不重新写入来恢复输出。 |

四工具目前不在 Code Mode 的共享白名单，不能提出通过 JS `tools.search_memory/get_memory` 补位。直接记忆写入的 canonical 回执也不走普通 artifact 发布；因此对这些回执不能假设被折叠后总有 artifact 可追回。

已确认的源头截取：get 的直接关系最多 100 条，没有 continuation；搜索矛盾关系默认最多 64 条，还会受 exposure 最多 50 个 ID 的过滤。artifact 只能保存截取后的结果，不能恢复其余关系。先修正完整性表达；若产品要求枚举全部直接关系，再在现有 query owner 内加稳定 keyset 续读，不加关系总数上限。

单记忆正文已有 8,192 UTF-8 bytes 边界，偏好正文为 2,048 bytes；无需再发明大文档分页。`remember` 最多回带 3 条完整 related 正文，可能使回执很大，应先精简它们，保证主要 status/target 完整送达。旧 search/get 正文不能从当前记忆重建，因为用户可能已经编辑或删除它；读取保存结果也必须保留原 exposure，与导出文件不等于已见正文的语义。

证据：[记忆工具 owner](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/memory_tools.py:277)、[直接关系截取](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/memory/recall.py:564)、[矛盾关系截取](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/memory/recall.py:494)、[直接写入结算](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_execution.py:2183)、[Code Mode 白名单](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_surface.py:236)。

## 10. artifact 与 Cell：5 个

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `read_artifact` | 删除工具与 ARTIFACT 身份，由 `read_file` 精确读取原结果正文；使用完整 next，不要求模型计算字符位置。 | source coverage、缺失/损坏/权限错误、正常 EOF，不重执行原操作。 |
| `export_artifact` | 删除工具，由 `write_file(target=PATH, source=原结果)` 复制完整正文；不要求先 read。 | 独立目标写权限、精确原文、原子 no-clobber、写前回执预算检查及发布不明的真实语义。 |
| `exec_cell` | 保留 code/path XOR 和 accepted CELL，不加日志页参数。 | 接纳/完成区别、原执行源码与正式输出入队边界。 |
| `query_cells` | 保留 live 列表和冻结源码两分支；改用完整消息实际计量，避免 raw JSON 预算错误；真实防套页，给完整 next。 | 不消费输出，源码不可用明确报告，Cell 生命周期与权限。 |
| `manage_cell` | wait/stop 参数保持，不增加输出读取或 offset。 | wait 只观察终态/超时；stop 不撤销已接受 Work；正式输出仍由原队列交付。 |

完整保存正文及范围受控的读取/导出有独立价值，但不需要 artifact 独立身份或专用工具。结果来源解析复用原接受结果 owner；不将它并入带冷历史范围、邻接和方向语义的历史浏览器。正式 Cell 输出是既有 input/content，不借此增加内部结果枚举或自动向 ROOT 披露。

证据：[artifact 页与导出](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/artifact.py:49)、[Cell 页面](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_runtime.py:3072)、[JS 值交付与边界](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/code_mode/host.py:466)。

## 11. 其余工具：8 个

| 工具 | 建议修改 | 必须保留 |
| --- | --- | --- |
| `open_in_pulsara` | 完整展示载荷执行前准入；超出本次预算时明确要求拆输入。不给展示控制加结果分页，不能依赖 artifact 才触发打开。 | targets 顺序、逐项 errors、最后选中项；不新增任意 maxItems。前端目前只执行 COMPLETE 展示结果。 |
| `view_image` | 不为分页改参数或交付；继续整体图片准入和 IMAGE_REF。 | 单次图片事实、真实多模态 FULL，不降为 Base64 文本，不静默新增批量参数。 |
| `edit_file` | 成功状态/target/新 revision/修改数量保持完整；完整 diff 沿保存正文读取，changed_windows 省略时给当前源行范围入口。 | 同次文件只改一次；读当前文件不能恢复本次完整 diff。窗口未实际完整交付不应被计为已观察行。 |
| `write_file` | 目标仍为 PATH；内容可由 `content` 或 `source=原结果` 二选一提供。source 总是完整冻结正文，不是当前页，也不经过模型/JS 桥接。短回执不加分页。 | 源读取与目标写入分别授权；create-only、原子 no-clobber、真实写入确认/不确定结算，不因显示问题再次创建。 |
| `todo` | 保留计数型短回执，不加分页或查询 owner。 | 原 checklist 合同与安装时机，不新增持久状态。 |
| `enter_plan` | 保留短控制结果，不加分页。 | 独占执行、规划状态与 continuation 一致性。 |
| `ask_plan_question` | 保留完整用户答案和现有输入预算准备，不把决定性回答拆为业务结果页。 | OPEN/ANSWERED 事务边界，不因显示失败重复询问或接受答案。 |
| `exit_plan` | 不加结果分页；工具结果本来只含控制状态，长 proposal 是用户审阅正文。 | 原批准/修订/取消语义，UI 已有独立正文读取；不能把大输入误判为大结果。 |

证据：[展示工具](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/presentation.py:80)、[前端 COMPLETE 要求](/Users/plumliu/Desktop/python_workspace/pulsara_agent/frontend/lib/runtime-adapter.ts:3099)、[编辑结果](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/tools/builtins/filesystem.py:777)、[规划控制投影](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/primitives/tool_result_projection.py:186)。

## 12. 建议实施顺序与必要验证

1. 先闭合每调用 allowance、实际消息计量和完整交付，修 `query_cells` 的可复现计量问题。同步替换每 FULL 调用最坏 40k 展示预留，不能只扩大 FULL 分类；源正常页不再交给二次头尾裁剪。
2. 修文件读取/搜索的预算、500 字符静默截取、单行死路和实际续页；保留文件观察与 Code Mode 交付时机。
3. 统一 terminal 的一次显示决策及 native/JS 正文；把旧观察原文和后续新输出明确分开。
4. 精简 Work/schedule 等列表，保护 mutation/control 成功事实；对 memory 回执及 open 控制等分别验证，不能一刀切全工具输出形状。
5. 统一可复制的 next/read 指引、工具描述和前端范围文案，完整切到共享 read/write 路径，删除 artifact 专用工具与身份。各步骤是逻辑拆解，不保留新旧双路径上线。

最小验证围绕真实风险：多字节及转义后的最终消息大小；完整 next 能推进；源页与保存正文末尾不混淆；超长单条有明确路径；Code Mode 对截断值的处理不冒充全量；写入/控制已经成功后显示失败不重复执行；真实权限、取消及 provider prefix 不变。生产 `_query_cells` 的内存复现不能替代这些端到端验证。

## 13. 需逐项决定的产品语义

- 搜索推荐不返回精确全局总数，count 模式仅保持每文件匹配行数；位置续查与其明确代价见第 15 节，待正式规格采纳。
- 动态源分页是否承诺某个时点的完整快照；当前多数目录没有这一承诺，不能为统一悄悄增加快照体系。
- 超长单行是否要求片段累计后授予行编辑观察；不应将 artifact 阅读自动等同原文件观察。
- 记忆关系是否需要模型穷尽枚举，还是明确的相关关系投影；不要将 top-k 召回误改成全库分页。
- 当前推荐已选择将正文归原结果并共享 read/write；正式规格仍需列明可接受的 entry 类型与已有 scope，不额外创建披露 ACL 或注册表。

视频工具仍未接入；其 `query/offset` 取舍与默认时间范围尚待单独冻结。本报告没有将此前 3/10 分钟等讨论性数值写入规格，也没有修改视频实现规格。

## 14. 推荐算法：一次结果、共享读写、自动装页

本节是讨论结论，不是已批准的实现规格。选择标准为简单、高效、模型认知难度低；权限、不可重执行的副作用、provider prefix 连续性是必须满足的约束。没有为了兼容现有数据保留机制。

### 14.1 五条候选路径

| 路径 | 优点 | 不选择的原因或必要代价 |
| --- | --- | --- |
| 保留 artifact 两工具 | 精确正文可读、可导出 | 仍有 result/artifact 两个身份，以及另一组读写操作。 |
| 删除身份，换为两个结果专用工具 | 冻结准确、scope 清楚、无文件副本 | 能解决问题，但模型仍需两种额外操作；共享读写可提供相同能力。 |
| 大输出自动成为普通文件 | 模型熟悉；rg/jq 可少一次导出 | 自动产生真实文件，改变原结果的作用域与明确导出边界。若保留原 scope，必须进一步约束所有文件/进程消费者，不能靠路径难猜或只读位。 |
| 删除完整输出保存 | 存储最省 | 一次性结果和被驱逐日志不能补回；要求模型提前重定向或重做命令不能成立。 |
| **共享 read/write，正文归原结果** | **少两个工具和一个身份；无自动文件副本；保留精确补读和明确导出** | **读取工具多一种来源，写入工具多一种内容输入；比增加专用工具族更容易解释。** |

普通文件方案的优势是真实的，不因代码量否决。推荐共享读写，是因为模型获得其主要减法，又无需默认把所有大结果公开成 host 文件。需要 shell 批处理时多一次明确保存调用，具有独立用途。

### 14.2 只保留三个模型动作

1. **继续取源数据：** 原工具返回完整一页与完整 `next` 调用。
2. **补读这次已产生的正文：** `read_file` 读取给出的原结果 target，再返回完整 `next`。
3. **需要实际文件：** `write_file(target=PATH, source=原结果)` 保存整个正文。

不需要模型学 ARTIFACT、推算 offset、选择传输页大小、拼接分页结果再重发给写文件工具。`read_file` 的命名是否改为 `read_content` 可在正式规格中决定，不改变算法；不要同时保留别名。

以下只是调用形状示意，ID 由真实响应提供，不由模型拼装：

```text
read_file(target = 工具给出的结果来源)

write_file(
  target = PATH("logs/build.log"),
  source = 工具给出的结果来源
)
```

结果来源复用现有 SESSION_ENTRY 身份，精确接受哪些 entry kind 由同一 source resolver 检查。它不是聊天历史遍历，不读取邻居，不采用 cold-history floor/方向/include_tools，也不因此变成 ROOT-only。

`source` 的值统一为 Target 对象，直接复用 `read_file.target` 中的结果 Target 分支（`kind=SESSION_ENTRY`，携带原 session/entry 身份），不设计另一套 Source 引用格式。工具返回的结果 Target 可原样用于 `read_file.target` 或 `write_file.source`。`write_file.target` 仍是目的文件 PATH；`content/source` 严格二选一。来源类型一致不改变可读 entry 范围及权限，也不自动增加普通 PATH 复制功能。

```text
savedTarget = 工具返回的 SESSION_ENTRY Target

read_file({ target: savedTarget })
write_file({ target: PATH("logs/build.log"), source: savedTarget })
```

### 14.3 正文和展示分开，但正文只有一份权威

每次真实执行只确定一次公开正文，并与原接受结果关联。没有 8,000-byte 归档阈值，没有额外 ARTIFACT ID、结果注册表或第二套正文权威。短正文可 inline，长正文复用现有 blob；不是每次结果都新写一个 blob 或普通文件。

正文的定义由业务 owner 固定，不随展示大小改变：终端是同锁选中范围的完整公开日志；MCP 是本次实际取得的公开响应的固定表示。它不是带行号的读取响应，不是 head/tail，不含展示 footer。控制状态、exit code、target、覆盖事实由结果的结构化元数据保留。JSON 仅序列化一次，补读/保存不再 pretty-print 或改变原字节。

源尚未返回的数据、被丢弃的日志、远端从未提供的字段不属于“完整正文”。存储不可用或达到既有物理边界时明确报告；不能伪称完整，也不能重执行原业务恢复。

### 14.4 只做一次展示决定

```text
源分页工具：
  在本调用预算内形成完整源页
  -> 本页 + source next
  -> 不再套保存正文分页

一次性结果：
  执行一次 -> 接受完整公开正文与真实业务状态
  -> 正文可容纳：直接完整显示
  -> 正文过长：显示预算内预览 + read_file 完整调用

补读结果或普通文件：
  读取当前文本页 -> 本页 + read_file next

需要真实文件：
  write_file(source=原结果)
  -> owner 内复制整个正文 -> 简短创建回执
```

只有实际省略时才出现补读 `read`。已经完整显示的小结果无需附加补读教程。源页的 `next` 和预览展开的 `read` 含义不同，不把两者同时当作读完该页的必经步骤。

通用层不再擅自从完整体重建第二种预览。成功状态、目标、退出码等不能在普通文本裁剪中消失。列表先精简大正文；大控制结果仍保留当次真实结果，不通过再次 stop/delete/reload 获取所谓下一页。

### 14.5 文本统一按行及行内位置续读

PATH 首次读取可提供任务所需行范围；结果来源默认从头。两类来源共享同一个文本 packer：优先完整行，单行过长时切 Unicode 字符片段，续点为行号与行内位置。不要让同名 offset 一会儿表示行、一会儿表示字符。

实际续点、revision、剩余语义范围由完整 `next={tool, arguments}` 携带，模型原样复制。结束为 `next:null`，不再重复暴露三套 has_more/next_offset/next。首次调用不需要填写 continuation，也不新增持久 cursor 注册表。

PATH 在续读时核对原 revision；变化则明确 stale，不拼接两版。只有实际完整交付的行授予文件编辑观察；片段累计在原进程内 observation owner 完成，不增加持久回执。结果来源冻结、独立校验 scope，永远不授 PATH 编辑观察。保存出来的真实文件走正常 PATH 读取后才授观察。

列表记录保持原子性；最小记录或 metadata 放不下时明确资源边界，不能跳项或成功返回同位置 next。EOF 与无法前进是不同结果。

### 14.6 预算算法同时改，不能只全改 FULL

目前 runner 在执行前对每个 FULL_REQUIRED 调用使用近 40k 的最坏展示上界；这会把小请求批次也过度预留。完整页设计必须同步改变该准入路径。

建议复用现有 image resource allowance/quote owner 的思想，并扩展到文本：

1. 保留 canonical、epoch、items 的真实物理上界预留。
2. 得到本批真实调用后，以冻结 prefix、实际 assistant/replay 和各调用必要小回执构造 followup base。文本展示不再每次预占填满 40k 的最坏 body。
3. 由已有 provider quote owner 算可用 wire/token 资源，按既有等分方式给需要展示内容的调用分配 allowance。无字符/token 固定换算，不加新的调度、抢占或持久预算系统。
4. 工具按自身语义形成候选页；使用已有完整 Q 和 provider quote 校验，包括转义、timing、来源与完整 next。文本候选替换该调用 base body，不是再新增一条 body。
5. 完整候选先试；不适配则对可切文本或记录前缀缩小，最终实际序列化验证。分页大小由 runtime 决定，有独立任务用途的行范围、top-k、少量终端观察等参数继续保留。
6. 已在 canonical/logical slot 内预留的文本不重复算作附件增量。增量组合安全沿现有 quote owner 验证，不能自行假定 token 差值总可相加。
7. 全批最终 suffix 再经原 owner 实测；安装的页面完整、冻结，不在后续编译中重新裁剪。余量不再进行第二轮回收扩页，减少调度和不确定性。

40k 是现有单结果逻辑上界，不是每次必须填满的目标。展示默认仍是产品策略；本报告不凭空选一个“最优字符数”。必要小回执都容不下时，在物理操作前走真实资源边界或允许的 compaction；操作已接受后保留真实状态和原结果，不重执行。后续动态上下文增加仍由原最终预算/compaction owner 处理，不能重装当前 prefix。

Q 的二分不是零成本：每次重新序列化后验证，约为页大小乘迭代次数。先线性收集有界候选再校验临界前缀，避免逐条重序列化导致平方复制。任意 continuation 长度和 EOF metadata 变化都必须包含在最终实测中，不能只拿原文长度推算。

证据：[现有 allowance owner](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:395)、[现有分配入口](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:2829)、[当前 FULL 最坏准入](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/runner.py:2616)。这是待实现的预算扩展，不宣称当前所有文本调用已取得 allowance。

### 14.7 终端与 Code Mode

- 终端默认展示 tail，保留有独立用途的少量观察限制；通用层不再展开全文或改成另一份 head/tail。
- 跟随后续日志的完整调用携带本次 through cursor；补读本次冻结日志则调用 `read_file`。进程新观察不叫保存正文的 next。
- write/submit 从写前切点返回新 delta，close_stdin/kill 返回实际控制状态，避免重复归档整段旧日志。既有 wait、PTY、物理退出与取消结算不变。
- Code Mode 直接得到本次完整 public value，源分页仍只是本次实际源页。它无需为统一展示而先写文件再读，也不应被强制仅得到 native tail。
- 完整值超过既有 4 MiB 桥时，明确返回可捕获的传输边界和已接受结果的准确 read 调用；不能将已成功业务改称执行失败，更不能重做。
- 显式 console/return 才进入 ROOT 的正式输出；不自动投递或枚举内部结果。当前精确读取的 scope 是 session/workspace，不伪称已有披露 ACL，也不新造披露注册表。
- `write_file(source=entry)` 在 owner 内复制全文，绕开的是模型搬运与 JS 桥接，不是源读取权限或目标写权限。

### 14.8 验收应证明的用户体验

1. 一页源结果只调用一次即可读完，next 原样调用能前进；批量小查询不再因每次最坏 40k 展示预留被错误挡住。
2. 一个超长单行无须模型调小 limit 猜测，能够持续读到末尾；PATH revision 变化明确失败。
3. 大构建日志可补读中间内容，不重跑构建；随后新日志与旧正文补读分明。
4. 将保存正文写文件只需一次 write，不需先逐页读进模型再拼接；输出字节等于冻结正文。
5. 小回执保留真实成功状态和目标；大数据源不因读完保存体就被宣称全部读完。
6. Code Mode 能直接筛完整值；桥接溢出可补读，业务效果不重复。
7. 连续分页、批量工具与允许的 compaction successor 长程可运行；不新增总页数、总调用数或历史上限。

另外必须针对已发现的缺陷逐项验收：

- 批量小查询：按实际调用 allowance 交付完整小页，验证不会被每调用最坏 40k 的 provider 展示预留误拒；必要资源不足时明确边界，既有物理准入仍有效。
- 搜索：命中位于第 500 字符之后时，返回的 excerpt 必须包含真实命中且明确是片段；完整当前行可经 read_file 读到。源变化不伪装恢复旧行。结果保存不得声称包含从未保留的完整行。
- 搜索与查文件：大结果不会先在 Pulsara 中无界收集全量 stdout 再只显示一页；必须分别记录峰值驻留和跨页重新扫描的成本。仅把 subprocess 改为流式读取，不能宣称消除后续页重扫。
- Cell 与所有共享 packer：反斜线、引号、控制字符、中文及完整 next 一起进入最终消息计量。以内部 JSON 39,671 bytes、完整 Q 79,425 的已复现案例作为回归输入，验证返回页确实满足完整工具消息边界并可继续，无字符丢失或重复。

尚需正式规格冻结的细节是 source target 的精确 entry 类型、模型字段命名和默认展示策略。推荐路径已明确，但最优性仍需通过真实 provider 的调用次数、输入 token、等待时间及模型能否正确照抄 next 验证；本轮没有运行这些验证。

### 14.9 已发现问题的覆盖程度

| 问题 | E 中的处理 | 当前设计状态 |
| --- | --- | --- |
| 每 FULL 调用最坏 40k provider 展示预留 | 扩展现有 allowance/quote owner，按本批实际可用资源分配展示，最终整批校验；保持独立物理边界 | 算法要求明确，尚未实现验证。 |
| query_cells 只量内部 JSON，漏掉外层转义 | 所有文本/记录页统一使用完整 Q 与实际 provider quote，元数据和 next 一起计量 | 算法要求与回归输入明确，尚未实现。 |
| 搜索行前 500 字符静默丢弃 | 搜索明确返回命中摘录；完整当前源用共享 read_file，必要的长行片段也能继续 | 推荐语义明确，尚未实现；不承诺重建搜索当时未保存的整行。 |
| 完整收集搜索结果后切页，每页重新搜索 | 第 15 节的文件位置续查：只重枚举路径，跳过已完成文件内容；删除默认精确全局总数 | 推荐算法已明确，尚未实施；当前巨文件跨页仍重扫，须实测成本，不宣称彻底消除所有重扫。 |

E 的共享读取/写入接口本身不能自动修复任意源工具。输出预算、页交付、源生产和检索语义必须分别满足合同；第 15 节补齐推荐的搜索生产方式，仍不代表代码已经完成。

## 15. 搜索续查：文件位置推进，不保活全局搜索

本节由 4 个 sol / high 代理与主代理交叉讨论后形成，仍是推荐方案。目标是普通定位查询早返回、完成文件不反复扫描内容、模型只复制 next；不以代码修改量或旧数据兼容作为取舍依据。

### 15.1 推荐路径及代价

每页重新枚举当前合格文件，但跳过已完成文件的内容搜索；只对未完成的游标文件及后续文件运行 rg。页结束时所有本页 producer 都退出，不留下等待下一轮模型的进程或搜索会话。

| 候选 | 收益 | 不选择的原因 |
| --- | --- | --- |
| 每页对 root 完整重搜后再切页 | 状态最少 | 先前所有文件内容都反复搜索，未解决当前主要成本。 |
| 保留同一个 rg 流 | 早返回，内容扫描一次 | 模型不继续时仍占进程/FD；若持续 drain 到磁盘，则变成后台全扫和 spool 生命周期。 |
| 首屏前冻结全部结果 | 跨页稳定，内容扫描一次 | 首屏先等全扫，保存未必使用的全部命中；现有单 blob 边界不能变成整个搜索的总结果上限。 |
| **按文件位置续查** | **早返回，无闲置 producer，完成文件不再内容搜索，无全量结果存储** | **路径元数据重新枚举；当前巨文件多页时仍重搜；页末批次可能有少量超前读取。** |

对静态目录可持续遍历到结束，不新增结果总数、总页数、查询寿命或重试次数上限。对变化目录是逐页观察，不保证某一瞬间的全目录快照。

### 15.2 一页怎样产生

1. 解析并授权原 root，在该 root 用内置 `rg --files --null --sort path` 流式枚举候选。用户 glob 继续交给已有 wcmatch，不另写 glob 解析器。
2. continuation 的边界由原 root 相对的遍历路径及匹配行位置表示。跳过已完成文件，仅游标文件需要重新搜索并丢弃已交付行。展示 PATH 的 resolve 结果不作为遍历排序 key。
3. 将遍历中连续属于同一父目录的候选组成批次；批次受本页需求和真实 OS argv 资源约束。不能跨子目录把同父目录的文件全部合并而改变顺序。
4. 内容搜索保持目录方式：`cwd=parent`，参数 target 固定为 `.`，使用 `--max-depth 1 --sort path` 与每个 eligible basename 的锚定 literal glob。转义复用 `wcmatch.glob.escape(..., unix=True)`。不能改成 explicit FILE 参数冒称语义等价。
5. 单个 rg record 解码后立即生成定位摘录并交给 E 的页 packer，按本调用 allowance 和完整 Q 装页。文件路径、count 等记录保持原子性。
6. 达到页预算或语义范围后停止本调用 producer，关闭管道并等待物理退出；返回完整的原工具 next。没有跨调用 child、枚举器或后台全量缓存。

原请求本身就是显式单文件时，保持其显式文件分支，而不是强行转目录搜索。目录策略与单文件策略各自沿依赖原有行为。

### 15.3 模型只复制 next

初始调用仍提供查询条件；续查调用的完整 next 包含原操作、查询、已解析 root、glob、模式、位置和单次条数上限。模型不计算 offset，也不管理 search ID、PROCESS 或关闭动作。

若当前页停在 `b.py` 第 120 行，下一页跳过排在它之前、已完成的文件内容；仅重新搜索 `b.py` 并忽略已交付行，再处理后续文件。这里没有保存体读取层，next 始终指向原 `search_content` 或 `find_files`。

next 表示还有扫描空间，**不保证已知还有一条真实命中**。页满不为一次 lookahead 扫描巨大稀疏后缀；最终可能有一次空页加 `next:null`。只有确实正常走到序尾才报告结束。

### 15.4 模式、排序和计数

- `content`：每条匹配行返回围绕实际 submatch 的明确摘录和原文件行位置；长匹配只展示窗口并标明片段性质。完整当前源经 read_file 取得，不假装保留搜索当时从未保存的整行。
- `files_only`：每个匹配文件一个记录，继续点越过该文件。
- `count`：每条记录是该文件的匹配行数，需完成该文件搜索。它不是 regex occurrence 数，也不是全库总数。
- `find_files`：只枚举和筛选路径，不运行内容搜索。
- 删除默认精确全局 total_count，避免首屏仅为数字被迫扫描到 EOF。确实需要全量统计的任务可沿 next 遍历并汇总。
- 顺序明确采用 rg 的原生目录组件遍历序，替代旧 Python 全路径字符串排序。例：`a/b` 可位于 `a+` 之前。游标比较必须与这个顺序一致，不能使用会隐式 case-fold 或 resolve 的展示对象。
- `limit` 保持单次请求最多返回多少记录的含义，实际还可因 runtime 预算减页；next 沿相同上限继续。用户只要 5 条可请求首屏最多 5 条并停止；想遍历全部则继续 next。不能将现有默认 50、单次最大 1,000 偷换成整条查询的总结果上限，也不额外维护 remaining-total。

### 15.5 变化、重复读取、错误和取消

- continuation 是位置，不是消费型流，也不授予权限。每次重新检查原 root 和当前调用 scope；首屏参数与不一致的 continuation 混用应明确报错。
- 重复同一个 next 是从同一边界重新观察。源和有效页预算相同才会形成相同语义页；源或预算变化可改变页长度，不能承诺精确旧页重放。每次使用实际采用页返回的新 next。
- 游标文件被删除时，从严格大于该遍历边界的文件继续，不将它误认为 EOF；root 本身消失返回源错误。插入到已越过位置的结果可能不再出现，改名到后面可能再次出现，这是 live 查询边界。
- 页满的内部主动停止不等于源 EOF。必须 drain stderr 并物理 join；真实失败不能被预期终止信号掩盖，不自写 rg stderr 文本分类器。
- 搜索默认 best effort：局部不可读文件或 stderr 诊断不阻断整页，有效命中照常返回，保留诊断并继续后续范围。扫描可能不完整时明确标记 PARTIAL，不将它冒充全源完整；不增加 strict/best-effort 参数或 stderr 文本分类器。PARTIAL 描述源扫描覆盖，不表示本页可以少交付字段；本页仍须按 E 完整交付。
- 查询本身无法执行、解析失败、超时或取消不能伪装成成功推进；原 continuation 可重试，先前已经成功交付的页不撤回。无诊断的页切出不证明 EOF，只有自然正常结束才能据此声明当前源穷尽；允许 PARTIAL 时还需保留整条续查的未完整事实。
- 删除当前第三次重复查询警告、第四次阻断。合法重读不应被当成查询次数上限。
- 没有进程内搜索游标过期；重启后只要来源和权限仍有效，就能从自含位置继续。

### 15.6 生命周期与实际复杂度

复用现有 IO 执行与并发 owner，每页私有路径 producer 和最多一个活动内容 producer；取消必须实际终止并 join 子进程，不能只取消 asyncio/to_thread 包装。用户不翻页时不遗留工作。并行查询继续排队执行，不增加任意 active-query 总量限制。

设页数为 P、路径量为 F：路径元数据仍约重复枚举 P 次，还包括父目录批次的直接子项。内容工作为后继文件搜索、游标文件重搜，以及页末批次的可能超前读取；不能声称每个文件在物理上恰好只读一次。一个巨文件横跨许多页时，仍可能重复搜索它的前缀。

不使用 `tail | rg` 或原文件字节 seek 假装消除重扫，否则会改变正则锚点、BOM、转码和行号语义。

Pulsara 驻留为页、argv/路径批次、管道缓存和**最大单条 rg JSON record**；不是严格 O(page)。逐 record 使用成熟 JSON 解码后立即释放，避免整份 stdout 与全部匹配常驻。rg 自身也可能需要容纳完整单行。不新增任意 record 总字符 cap，不为理论常量内存另造 JSON/编码框架；用最大单行峰值验收真实物理成本。

### 15.7 已验证与实施验收

本轮仅检查私有 rg 15.2.0 的帮助和局部 CLI 行为，并使用临时夹具/现有文件验证，未修改生产代码、数据库或配置，未调用 provider。

已核实：

- `--sort path` 为目录组件顺序且单线程，不等于旧全路径字符串排序。
- `--max-count` 是每文件上限，不能当成全局页 limit。
- explicit FILE 与目录搜索对 binary/symlink 的行为有差别，`--no-binary` 不能自动消除此差别。
- 锚定 basename whitelist 必须配 `cwd=parent,target='.'`；仅改 cwd 而仍传绝对 parent 会出现选不中的情况，不能将零匹配误当 binary 验证成功。
- 正确批次形状下文本 content/files_only/count 均匹配，目录 binary 行为得到正负对照；特殊 basename 的现有库转义可交给真实 rg 解析。

实施必须覆盖：稳定源多页无遗漏、命中在第 500 字符之后、巨型单行峰值、游标文件删改与改名、重复 next 和不同页面预算、source/模式冲突、最后空页、局部错误保留命中与诊断且可继续、无法执行时不伪装推进、末页保留 PARTIAL、页满停止/取消/重启无遗留进程、精确每文件 count、ignore/hidden/binary/symlink 及 Windows/Ubuntu/WSL 路径转义矩阵。用内容扫描计数证明已完成文件没有再次提交内容搜索，而非仅比较最终输出。

当前推荐接受游标巨文件的跨页重扫，换取无常驻搜索生命周期和无全量冻结开销；不添加自动新旧算法切换路径。若未来实际负载证明这项成本不可接受，再提出具有独立需求的另一版规格。

## 16. 实施前就绪检查

主路线已明确。当前文件仍是审阅与推荐算法，不应直接当作已冻结的完整实现规格。以下是实施前应补齐的合同，不是要求增加新框架。

### 16.1 必须先落实到规格的技术合同

1. **正文、回执与精确来源。** 对每类工具写明唯一可读取/保存的正文：终端是选中日志，MCP 是本次公开响应，编辑是实际 diff 等；必要业务事实独立完整交付。冻结可接受的 tool-result/正式 Cell-output entry 类型和原 scope，不开放任意聊天消息及邻居。明确短正文 inline、长正文 blob、存储不可用和源已有 gap；直接记忆写入等事务内最终结果同样纳入，不能只改普通执行分支。
2. **预算与接受事务。** 将每调用最小回执、展示 allowance、正常/晚到 carrier、完整 Q、provider wire/token 和最终批次复核写为一条执行链。特别验证：最低结果都容不下时不开始新的物理副作用；业务已接受后不能因展示或桥接失败改称未执行，亦不得重新执行；已有 prefix 不因新页预算而改写。
3. **完整工具接口及一次切换。** 冻结 read_file 的两类 source、统一文本 continuation、write_file 的 content/source XOR、next/read 字段和 JS 边界错误形状。删除 artifact 工具、身份、描述、UI 依赖和旧分支；合法新 epoch 激活新工具表。不以兼容当前数据保留别名或双路径。

这些是实施设计工作，主代理可完成，无需将每个字段命名都转交用户决定。预算链先用已有 owner 做集成验证，再扩散到全部工具，仍以完整 hard cut 作为最终交付。

### 16.2 已确认的产品行为

- **搜索默认 best effort。** 保留有效命中、明确 PARTIAL/诊断、继续后续范围；不把局部读取失败当作全局无命中，也不让普通 warning 卡住同一个 next。continuation 保留本链已观察到的未完整事实；末页 next 为 null 也不将 PARTIAL 改成全源完整。查询无法执行、解析失败、取消等不能伪装成正常推进。不增加模式参数。
- **source 使用共享 Target。** `write_file.source` 直接接受与 `read_file.target` 一致的结果 Target 对象，目标仍为 PATH；没有独立 Source 类型或新的身份。源权限与目标写入权限分别检查，复制的是完整冻结正文。

### 16.3 可按默认处理，不阻断主重构

- 保留 read_file/write_file 名字，描述扩展到已有结果来源；不另造别名。
- 预览量不是单结果物理上界。先保留现有展示量级作为软默认，用真实 provider 的 token、等待和补读次数调优，不拍新的总输出/历史 cap，也不让模型必填页大小。
- 不在本次加入跨页目录快照、永久搜索会话或任意旧聊天正文读取。
- 记忆关系暂不顺带扩成全图浏览；已有关系截取必须如实标注局部性。是否增加穷尽关系遍历作为独立产品需求处理。
- 巨文件重扫、最大单条 rg record 内存、native/JS 交付、跨平台 basename/ignore/binary 和取消物理清理属于必须验证的风险，验证失败再修算法，不要求用户预先选择技术实现。

实施完成的激活门槛仍包括真实 provider 下的批量小查询、逃逸字符、直接记忆写回执、写入后桥接失败不重做、连续分页跨 compaction、正确复制 next，以及既有源码/文件编辑观察与 scope 不变。本轮尚未实施这些验证。

## 17. Target 与 ID 减法清单

已核对当前生产引用及 Python/TypeScript/JS 统一身份层。此处是实施必须覆盖的范围，当前尚未执行生产代码删除。

### 17.1 Target 只减少，不新增替代身份

| Target / 身份 | 本次处理 |
| --- | --- |
| `ARTIFACT {artifact_id}` | 完整删除模型输入 schema、生成与校验、返回值、读取路由和所有消费者；不保留别名或旧 ID 映射。 |
| `SESSION_ENTRY {session_id?, entry_id}` | 复用已有结构；结果来源指原接受结果的 result_entry_id，不为正文或每一页另分配 entry。精确 source resolver 校验 entry 类型及原 scope。 |
| `PATH {path}` | 保留文件读取及写入目的地；不是 Pulsara 新实体，不另造 file/output ID。 |
| OUTPUT/TOOL_OUTPUT/RESULT 等替代 Target | 不引入；现有 TOOL_RESULT entry kind、result_id 与 OUTPUT 内容角色并不等于另一个 Target，保留其各自真实含义。 |

`read_file.target` 与 `write_file.source` 的结果分支引用同一个 Target 定义，不维护两份近似 schema。读取下一页仍指原结果，不能改指刚生成的读取回执，造成递归正文链。

### 17.2 artifact 必须删除的引用链

1. **公开工具和权限。** builtin catalog 的两个 ARTIFACT schema、read/export 工具、工具权限与 Code Mode whitelist、终端工具说明、bundled skill、错误恢复教程及前端标题/摘要分支。
2. **ID 词表和分配。** 从 Python IdentityKind 和共享 JS identityKinds 同步删除 `artifact`，TypeScript/renderer沿既有共享 re-export 更新；删除 `_artifact_id(session_id,result_entry_id)` 及其 require_id 分支，不新增 result→artifact→entry 转换。
3. **结果及 provider 投影。** 删除 artifact_id、专用 artifact disposition/阈值/模式、专用 FULL reason、附加 footer、REF_ONLY 的 artifact 分支等旧合同。源覆盖、实际正文是否可读及完整成功事实要由新单一路径承担，不随 ID 一起丢弃。
4. **Code Mode。** host/guest 的 `artifact` 返回及错误字段、晚到结果恢复值、桥接边界恢复指引同步改为原结果 Target，不能删入口却仍在错误中指导模型使用旧工具。
5. **数据库与 clean-v0。** 删除独立 output_artifact_id 列、ID check、唯一索引及旧投影字段；必要的正文 content/blob 引用保留并归原 result owner。同步 repository/reader/acceptance 合同、baseline 及生成的 expected catalog。没有独立 artifact 表可额外保留。
6. **前后端正文读取。** protobuf、gateway、HTTP 路由、TS adapter 和 UI 原文展开统一直接读 result entry。当前前端入口已经传 resultEntryId，后端还会查 artifact_id 再回读，必须去掉这层绕行并更新生成协议代码；保留用户展开原文的能力。
7. **验证。** 更新真实行为测试、identity vocabulary 一致性、fixtures 和 dogfood；只验证新单一路径。不为旧数据或旧 Target 保留兼容测试、双读或格式修复。

关键证据：[Python ID 词表](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/identity.py:33)、[共享 JS 词表](/Users/plumliu/Desktop/python_workspace/pulsara_agent/desktop/src/identity-core.mjs:2)、[artifact ID 派生](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/conversation_kernel/tool_artifacts.py:453)、[SQL ID 与索引](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/storage/migrations/sql/0000_conversation_kernel_baseline.sql:743)、[entry→artifact 绕行](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/terminal_protocol/v3_gateway.py:1378)、[前端已有 entry 入口](/Users/plumliu/Desktop/python_workspace/pulsara_agent/frontend/lib/runtime-adapter.ts:2796)。

### 17.3 不按关键词误删真实 owner

- `blob` 是通用不可变内容身份与存储，提示词和图片也使用，继续保留其验证及生命周期。不是把 artifact_id 偷换成一个新 blob Target。
- session、entry、result、call、request 是已经存在的不同身份；复用其原创建边界，不合并也不重分配。
- `PostgresConnectionLane.ARTIFACT` 目前还被通用 blob/prompt storage 使用，是资源通道而非 Target。保留真实资源 owner；若调整命名，不新增平行通道或删除其他内容读取。
- frontend trace kind='artifact' 当前也被普通操作使用，不是 artifact 实体；应按真实通用工具语义整理，不能直接删除普通工具卡片。
- migration manifest 中禁用旧 public relation 名称、历史文档和 release 构建产物中的 artifact 字样，不是活跃 Target 或允许的旧模型路径。不移除数据库减法检测规则，也不修改无关发布产物机制。

### 17.4 本方案没有新增 ID 种类

结果引用直接取已接受结果的 session_id/result_entry_id；正文 blob 继续使用原 blob owner。native 调用、读取回执、写入回执及协议请求自然沿现有创建流程使用既有 ID，无额外 output/search-query/cursor/allowance 身份。

continuation 仅为查询与位置数据，next 仅为派生调用参数，不分配页对象、查询会话或游标实体。内容 revision、行号、行内位置及进程序号保持原坐标/版本含义，不改成 ID。

所有确需创建的 Pulsara 身份仍唯一使用：Python 的 random_id/derive_id/require_id；TypeScript 的 randomIdentity/deriveIdentity/isCanonicalIdentity；JS/Code Mode 复用共享 identity-core。格式遵循共享实现，不复制编码或正则、不使用 UUID/时间戳/局部计数器拼对象 ID。

证据：[Python 统一层](/Users/plumliu/Desktop/python_workspace/pulsara_agent/src/pulsara_agent/identity.py:134)、[TypeScript 统一层](/Users/plumliu/Desktop/python_workspace/pulsara_agent/desktop/src/identity.ts:1)、[renderer re-export](/Users/plumliu/Desktop/python_workspace/pulsara_agent/frontend/lib/identity.ts:1)。
