# Pulsara 原生工具修订与使用教程草案

> 已归档。当前工具参数和用法见 [native_tools/](../native_tools/)。

MCP 首请求采用[非阻塞启动与不完整搜索合同](PULSARA_MCP_NONBLOCKING_STARTUP_IMPLEMENTATION_SPEC.zh.md)：后台发现不阻塞 provider；不完整搜索返回 ERROR 和已知可用结果，接受后的有效引用仍可调用。默认提示词只说明结果语义；ROOT 的 Code Mode 具体写法集中在 bundled Skill，Subagent 提示词不包含 Code Mode 内容。

日期：2026-11-12。

网页能力的当前合同见[网页工具草案](../native_tools/web.md)：web_search、web_fetch 已接入原生工具、ROOT JS、用户级配置和设置页。默认无 Key 免费池为 Firecrawl → Tavily；可选四家之一＋API Key，自有 Key 按对应账户正常计费，额度不足等实际失败时静默回退；Exa／DeepSeek 不进入免费池。只读／Plan 与 Subagent 沿既有权限和结算；DeepSeek 支持显式自动引用首个已保存官方连接。实施及验证见[报告](../output/web-20261117/REPORT.md)。

状态：各工具族的当前合同已接入生产代码，包含 read_file 原生 NEXT 续读与 write_file 完整结果导出，已完成 hardcut 跨层及真实 provider 验收。本文件记录已确定的参数、业务返回值、原生交付形状及 Code Mode 用法，也作为后续 bundled skill 的教程底稿。工具、数据库、SDK、提示词和 bundled skill 使用同一当前合同。

MCP 发现上下文遵循[MCP 初始服务目录规范](PULSARA_MCP_CATALOG_INCREMENTAL_IMPLEMENTATION_SPEC.zh.md)：合法新上下文在 Skill 目录后提供轻量已启用服务名称，接口与引用仍按任务查询；不增加工具数量或 API 边界。增量验收见[实施报告](../output/hardcut-20261116/mcp-catalog/REPORT.md)。 本增量最终 205 项相关测试通过、无跳过；保存的 DeepSeek 两协议完成 19 次真实请求、6 次 SDK MCP 调用和 13 对同 epoch 前缀核验，冷／压缩目录及引用复用均通过。

## 1. 范围与阅读方式

本文件覆盖 Pulsara 原生工具、对应 JS API 及仅 JS 工具。各工具已确定的读取或操作范围、参数、返回字段、错误含义和组合方式见下列分篇文档；NEXT 续读与完整结果导出的共同原则沿统一交付草案，具体接口与示例见文件工具草案。NEXT 分支仅原生，tools.read_file 只接受 PATH。

当前确定范围：

| 工具与分支 | 已确定合同 |
| --- | --- |
| read_file，PATH Target | 参数、完整业务值、meta/content 包装、状态、行号投影、原生与 JS 示例 |
| read_file，NEXT Target | 仅原生，唯一 target 参数原样接收 NEXT；meta.status 与实际正文 content，tool_result 始终指向原完整结果，next 指原结果再下一页；冻结 12k 续读保持图文顺序，object 可跨页，无重复正文或新的分页链，不提供 JS 分支 |
| write_file，PATH + content／tool_result_target | 两个互斥输入分支；content 沿普通文本校验，完整结果导出不因 NUL 或二进制扩展名拒绝；共用目标授权、原子不覆盖与取消结算，返回 PATH、meta.status、成功时实际 bytes_written 和简短 content，含原生／JS 示例及错误语义 |
| edit_file，PATH Target | 精确唯一替换、整文件替换、空文件规则、diff 返回、原生与 JS 示例、错误语义 |
| find_files，目录 PATH Target | target 在先的最简参数、Target 内容项、无匹配说明、原生与 JS 示例、扫描失败及取消 |
| search_file_content，PATH Target | target 在先的参数、三种输出模式、匹配行及非重叠匹配次数、object 内容项、原生与 JS 示例 |
| web_search | query、可选 count／include_domains／exclude_domains／after／before；域名排除优先并核对返回 URL，日期为引擎检索条件；四家共用 meta.status 和逐条 URL Target／标题／摘要／可选日期 object；自有 Key 优先，否则 Firecrawl → Tavily 免费池，回退保留全部条件，成功空结果不补搜；只读／Plan 可用，ROOT／Subagent 原生冻结 12k／NEXT、ROOT JS 完整返回 |
| web_fetch | 只传 URL target；返回请求目标、meta.status／可选 title 与实际提取的 Markdown text；复用网页免费池与可选自有 Key；完整保存实际提取结果，不保证覆盖网页全部原文，回退不拼接多家正文；只读／Plan 可用，ROOT／Subagent 原生冻结 12k／NEXT 不重新抓取、ROOT JS 完整返回；模型按需交错搜索与读取，不自动抓取全部搜索命中 |
| exec_process | 四个启动参数、工具状态与进程状态分离、运行／退出结果、实际输出及原生与 JS 示例；删除 observe 和公开游标；待观察日志透明暂存 |
| manage_process | wait、write、close_stdin、kill 四个动作；内部观察位置、完整待观察增量、控制动作、原生与 JS 示例及输入小实验 |
| query_processes | 可选 PROCESS Target、单查与列表共用的 object 记录、原生与 JS 示例，以及列表无记录与单查未找到的不同状态和说明 |
| query_agents | 可选 AGENT／WORK target；省略列 Agent，AGENT 列其全部 Work 摘要，WORK 读取指令、状态及实际结果；ROOT／Subagent 原生及 ROOT Code Mode JS；返回 meta.status 与逐条 object／必要 text，删除 cursor、max_items、inspect、root 和来源分页调用；原生冻结 12k／NEXT、JS 完整值，不在补页时刷新工作状态 |
| send_agent_message | 必填 AGENT target、message，支持 ROOT 别名；仅向接收方当前活动轮次接纳消息，不启动工作、不暂存给空闲或尚未启动的接收方；未发送为 ERROR，只返回 meta.status 与文字 content，删除 recipient、work、delivery，不返回顶层业务 Target；ROOT／Subagent 原生及 ROOT Code Mode JS，NEXT 不重复发送 |
| stop_agent | 必填 AGENT／WORK target；停止该 Agent 当前及当时已排队工作，或只停止指定 Work；仅 ROOT 原生及 Code Mode JS；返回 meta.status、成功时四个并列 Work 状态计数，以及 text／逐项 WORK target＋work_status 的 object，不返回顶层业务 Target；运行中取消、排队停止、依赖及已提交工具结算保持现有语义，统一冻结 12k／NEXT，不重复停止 |
| wait_message | Subagent 专用、仅原生；可选 timeout_seconds，默认 30、范围 0～300 秒，0 立即检查；收到消息或等待超时均为 SUCCESS，只返回 meta.status 与文字 content，删除公开 state；停止沿现有活动任务取消语义，消息正文仍经正常输入路径独立交付；冻结 12k／NEXT 不重新等待或消费消息 |
| tools.run_agent | ROOT 专用、仅 JS，数组分派；target、instruction、work_title、materials、depends_on，删除顶层 context；materials 混合 CONTEXT_MATERIAL（仅 kind／turns，1～3）与 WORK，同源项先按 depends_on 处理，其余 WORK 材料须已结束；新建／复用均可显式带入，历史随本次 Work 冻结；结果 meta 仅 status，逐项 object 使用 agent、work、work_status，删除旧三组平行数组及 inspect；完整返回，不使用原生 12k／NEXT |
| tools.wait_agent | ROOT 专用、仅 JS；互斥 works／agents、settle、timeout_seconds，删除 until；WORK 精确等终态，AGENT 等当前 idle 并可因选中发送者消息返回；meta.status／wait_reason、消息时的 message_sources，逐项 object 返回工作状态或 is_idle；成功结果全量按输入顺序，first 不裁剪，超时 SUCCESS，取消不停止工作、不强求末次读取，通知位置仅归 Cell；完整保存后 JS 全值返回，无原生分页 |
| tools.decide | ROOT 专用、仅 JS、bypass 权限；condition 与 inputs 必填，逐项独立判断；结果仅 meta.status 与单个 decisions object，空输入成功、失败走实际 err.result；available() 为 Cell 启动时冻结的本地配置检查；完整保存后返回，无原生分页，删除旧展示预算检查，其余底层语义保持现状 |
| search_sessions | query、lifecycle 参数、同域跨工作区范围、entry 优先与标题兜底，以及只含会话信息和命中来源的 object 记录；不附具体 entry |
| search_session_content | target、query 参数，单 ID 的 Q₁ Target 与 QA／QAQ 摘录，四种消息标签及原生／JS 完整返回；删除 include_tools 和搜索续查参数 |
| read_session_content | 可选的原文展开步骤，必填 target、正负 limit；SESSION 从 latest 向前读，单 ID 的 SESSION_ENTRY 从锚点按指定方向读；逐消息 object 及原生／JS 示例 |
| enter_plan | 仅原生、空对象参数、meta.status 与简短文字结果；保留模式切换、自动继续、重复进入成功及单独调用行为 |
| ask_plan_question | 仅原生，question 与可选 options，始终允许自由回答；结果只用 meta.status 和文字 content，10 分钟未作答返回 CANCELLED，由模型决定是否调用 exit_plan |
| exit_plan | 仅原生，可选 plan、summary；提交方案阻塞等待审批，只返回一次最终结果，修订／取消审批／十分钟超时均让当前只读轮继续，由模型决定是否退出，完整图文修订反馈另沿用户输入路径交付一次；批准或无方案退出才恢复权限并自动续接；Markdown 按 summary 命名、默认方案.md、重名用 (2) 顺延，写入失败不阻止审批，实际保存成功才返回顶层 PATH Target；无方案退出不等待审批、不创建文件 |
| remember | 仅 ROOT 原生，context_kind、kind、title、条件必填的 statement；创建独立记忆，返回 Target、updated_at、简短说明及最多三条完整相关候选 |
| query_memory | 仅 ROOT 原生，target 精确读取与 query／可选 limit 关键词查询互斥，target 放首位；返回完整记忆对象，原生沿统一 12k 分页 |
| manage_memory | 仅 ROOT 原生，target、action:edit／delete；edit 提供 statement／context_kind，修改成功和无变化返回 Target、updated_at 及简短说明，删除成功只返回简短说明；不推荐候选；并发旧值由 runtime 自动取得，无快照先提示查询 |
| todo | 仅原生、无 JS API；items 完整替换清单，最多 10 项，非空单行文字不重复且至多一个 in_progress；删除文本大小 cap；成功返回 meta.counts 和简短 content，失败／取消不返回计数 |
| query_schedules | 仅 ROOT 原生、无 JS API；可选 target 精确查询与可选 status:ALL／ACTIVE／PAUSED 列表筛选互斥，默认 ALL；固定当前会话，删除 session、cursor；content 使用统一完整计划 object，revision 只保留在内部；原生沿统一 12k 分页 |
| manage_schedule | 仅 ROOT 原生、固定当前会话；create／switch／update／pause／resume／delete 六个 action，revision 内部取得；新计划权限取创建 turn 冻结值，修改不提供权限参数；五种时间规则、15 分钟最小间隔和默认上海时区；pause／delete 取消该计划未消费输入，已运行回复继续；resume 不补跑或恢复已取消输入；成功简短返回，delete 不返回业务 Target |
| open_in_pulsara | 仅 ROOT 原生、无 JS API；targets 接受 PATH／URL／AGENT，逐项请求展示；结果仅 meta.status 和文字 content，任一目标失败为 ERROR，说明已接受项及原因，合法项仍继续；不回显目标或归属 ID，NEXT 不重触发展示 |
| exec_cell | 仅 ROOT 原生、无 JS API；PATH target 与 code 严格二选一，删除顶层 path；接纳成功返回 CELL Target、meta.status 和简短 content，不返回 accepted 或 Cell 状态；steer 继续独立交付，NEXT 不重新执行脚本 |
| query_cells | 仅 ROOT 原生、无 JS API；只保留可选 CELL target，列表返回 object 记录，单查返回状态、pending_calls 及完整冻结源码；删除 has_unread_output、公开游标和源码分页参数；完整结果按 12k／NEXT 交付，补页不刷新 Cell 或消费 steer |
| manage_cell | 仅 ROOT 原生、无 JS API；target、action:wait／stop，wait 可填 yield_time_ms；成功返回实际 cell_status、pending_calls 和必要文字；停止、已提交调用及各阶段 steer 处理完全保持当前底层语义，不撤销已发生效果或撤回已入队输出；NEXT 只读冻结结果 |
| view_image | ROOT 原生及 Code Mode JS API；必填 PATH／IMAGE_REF target，完整保存有序 text→image content；原生能力不足或缺图时 ERROR 和文字说明，JS 仍完整返回；console.log／return 递归识别任意层级 image 节点，局部失败保留其他内容；凭已知引用读图，不查 session／workspace 或来源资格，冻结 12k／NEXT 保持 |
| read_video_frames | ROOT 原生及 Code Mode JS API；仅 target、from、to、action、crop，action 为 overview／detail／motion，删除 mode 和 continuation；完整范围先生成保存，有序索引 object→image，原生 12k／NEXT，JS 完整值；cell_rect 为 [left,top,right,bottom]，现行选择和质量保持 |
| read_transcript | ROOT 原生及 Code Mode JS API；仅 target、from、to，删除 continuation、公开 media_version 及单次累计 180 秒 ASR 限制；按完整请求范围取得字幕或语音文字并保存，meta 保留状态、来源及实际语言，content 使用 text／逐段 object；原生冻结 12k／NEXT，JS 完整值，补页不读取媒体或发起 ASR |
| annotate_media | 仅 ROOT 原生、无 JS API；target、from、to、text 全部必填，追加时间标注；返回 PATH Target、meta.status 和简短 text，不回传标注全文或公开成功标记；保留内部文件路径／修改时间绑定及由调用参数读取标注的现有语义，NEXT 只读冻结结果、不重复添加标注 |
| query_capabilities，SKILL 分支 | 顶层 kind 在所有调用中必填，分类为 SKILL／HOOK／MCP／PLUGIN，不支持全种类列表；列表可选 scope、PLUGIN parent，精确查询仅 target、kind；SKILL Target 只含 kind、skill_path，PLUGIN parent 使用稳定 plugin_path；列表和精确查询共用 SkillRecord，保留名称、描述、来源和状态文字，删除状态／资格布尔字段、source_kind 筛选和旧 offset／limit；原生冻结 12k／NEXT、JS 完整值 |
| query_capabilities，HOOK 分支 | 必填 kind:HOOK；列表可选 scope、parent，精确查询只填 target、kind；本地和 Plugin Hook 统一使用 kind、config_path 的 HOOK_SOURCE Target，不公开来源、作用域或 Plugin ID；共用 target、status、definitions 记录，不在 Target 外重复路径，每条定义只含 event、matcher、command；详细配置按 target.config_path 用 read_file 读取；原生冻结 12k／NEXT、JS 完整值 |
| query_capabilities，MCP 分支 | 必填 kind:MCP；服务列表可选 scope、PLUGIN parent；MCP_CONNECTION 以 config_path＋文件内 server_id 定位，MCP_TOOL 以运行目录 server_id＋remote_tool_name 定位；本分支只公开连接和工具 Target，模型不接触运行实例 Target；服务保留名称、状态、可选 instructions 及 resource_count／resource_template_count，计数未知省略、确认没有才为 0，配置路径不重复；不展开 Prompt 或资源目录，资源按需交给 read_mcp_resource；HOST_OVERRIDE 保留内部未消费入口；原生冻结 12k／NEXT、JS 完整值 |
| query_capabilities，PLUGIN 分支 | 必填 kind:PLUGIN；列表可选 scope，精确查询仅 target、kind，Target 以稳定实例目录 plugin_path 定位；列表 object 给包概览及三类组件数量，精确查询仅 text 与组件 Target 交错，不嵌套组件清单或重复包自身 Target；组件交给 SKILL／HOOK／MCP 分支按需查询；原生冻结 12k／NEXT、JS 完整值 |
| call_mcp_tool | ROOT／子代理统一 meta，ROOT 适用 Code Mode 时可用 JS API；target:MCP_TOOL_REF（首位）与 arguments 必填；同 session 原生及各 Cell 共用引用，查询完整接纳后可调用，无整页／FULL 门槛及每 epoch 1,024 个引用上限；模型自行决定 NEXT 续读；保留发现与刷新，实际工具／目标变化使相应引用失效，无关变化不连坐，同目标普通重连可复用；错误说明派发事实，不自动重做；meta.status、content、可选 structuredContent 沿共同结果，原生 12k／NEXT、JS 完整值 |
| read_mcp_resource | ROOT／子代理原生，ROOT 适用 Code Mode 时有 JS API；仅必填 target，MCP_CONNECTION 列资源和模板，MCP_RESOURCE 读取具体 URI；删除公开模板 Target 读取分支及顶层 uri，不增加 action 或游标；目录用 object，读取用有序 text／image，图片 blob 形成真实 IMAGE_REF；只返回 meta.status／content，无业务顶层 Target 或 structuredContent；原生冻结 12k／NEXT、JS 完整值，补页不重新 listing 或请求资源 |
| reload_capabilities | 参数固定 {}，全量刷新当前能力来源；仅 ROOT 原生、bypass 权限调用，无 JS API；业务结果只保留 meta.status 和 text content，部分失败为 ERROR，写清已完成部分、失败路径和具体原因，未确认状态如实说明；取消保留已发生事实；原生冻结 12k／NEXT，补页不重新刷新 |
| manage_capability，INSTALL_LOOSE_SKILL | 仅 ROOT 原生、无 JS API；target 为源目录 PATH Target，必填 action、scope，删除 source_path、name、description；名称与说明取源 SKILL.md，安装启用设置沿现有规则，目标已存在时报错；返回安装后的 SKILL Target、meta.status 和简短 text；失败说明实际路径、原因及已发生效果，已安装但刷新失败为 ERROR 并保留真实 Target；原生冻结 12k／NEXT，补页不重装 |
| manage_capability，SET_LOOSE_SKILL_ENABLED | 仅 ROOT 原生、无 JS API；保留独立启停，enabled 为必填 boolean，true 开启、false 关闭；target 为最小 SKILL Target，成功返回同一 Target、SUCCESS 和简短 text，重复设置仍成功；删除重复业务字段及 current／adoption 包装，错误写明实际效果；原生冻结 12k／NEXT |
| manage_capability，REMOVE_LOOSE_SKILL | 仅 ROOT 原生、无 JS API；只填最小 SKILL target、action；删除范围限有效 Pulsara home 的 skills 和当前项目 .pulsara/skills，两处 .agents/skills 保留发现和启停机制，bundled／Plugin Skill 不由该动作删除；只清理确切路径的启用记录，保留其他副本及同名选择；返回仅 meta.status／text content；清理或刷新部分失败为 ERROR，写清实际效果、路径及原因；原生冻结 12k／NEXT，补页不重删 |
| manage_capability，TRUST_HOOK_SOURCE | 仅 ROOT 原生、无 JS API；target 为 kind＋config_path 的最小 HOOK_SOURCE Target，只填 target、action；本地／Plugin 共用，完整非空当前定义经用户审阅后保存信任，完全权限模式也不跳过；不自动开启来源或 Plugin；返回同一 Target、meta.status、简短 text，取消及保存／刷新失败说明实际效果；原生冻结 12k／NEXT，补页不重新审阅或信任 |
| manage_capability，REVOKE_HOOK_TRUST | 仅 ROOT 原生、无 JS API；只填最小 HOOK_SOURCE target、action；撤销信任而保留启用设置，不删除定义／脚本，不再完整信任审阅；返回同一 Target、meta.status、简短 text，已保存撤销但刷新失败按 ERROR 并说明；原生冻结 12k／NEXT，补页不重做 |
| manage_capability，SET_HOOK_SOURCE_ENABLED | 仅 ROOT 原生、无 JS API；最小 HOOK_SOURCE target、action、必填 boolean enabled；开启／关闭保留信任，开启不建立信任或启用 Plugin，重复设置仍成功；返回同一 Target、meta.status、简短 text，失败与已保存效果放 content；原生冻结 12k／NEXT，补页不重新启停；其余底层语义保持现状 |
| manage_capability，MCP 共用 config | ADD／UPDATE 共用公开配置字段；ADD 补默认，UPDATE 按明确字段局部更新（含嵌套 transport），env／Header 映射整体替换；两类连接遵守各自修改边界，Plugin 保留包定义且支持 config:null 恢复默认；不公开 display_name，新增使用 server_id；保留适用的用户高级配置及凭据，敏感或特殊认证交现有编辑器；独立及 Plugin 连接默认允许 ROOT 和子代理，用户可在能力页关闭；对象局部更新保留显式范围，Plugin config:null 一并移除范围覆盖并恢复此默认，模型不填 scope_policy；两个 action 见第 8、9 章 |
| manage_capability，ADD_LOCAL_MCP | 仅 ROOT 原生、无 JS API；最小 MCP_CONNECTION target、action、可选 config，提供时按共用 ADD 候选处理，省略打开新增编辑器；同键存在失败不覆盖；成功仅同一 Target、meta.status、简短 text；取消、并发、保存和后续失败说明实际效果，原生冻结 12k／NEXT，补页不新增或重开编辑器 |
| manage_capability，UPDATE_MCP_CONNECTION | 仅 ROOT 原生、无 JS API；独立／Plugin 共用最小 MCP_CONNECTION target、action、可选局部 config，省略打开预填编辑器，Plugin 可传 null 恢复默认；未填部分保持、保留包定义、用户高级及适用私密配置；内部并发由 runtime 在本次操作内核对；成功仅同一 Target、meta.status、简短 text，重复正常提交仍成功；缺失不自动新增，失败说明实际效果，原生冻结 12k／NEXT |
| manage_capability，REMOVE_LOCAL_MCP | 仅 ROOT 原生、无 JS API；只填最小 MCP_CONNECTION target、action，内部核对由 runtime 取得；凭据、OAuth、项目来源批准和运行连接清理完全保持现有底层语义，不强杀已接纳调用、不等待全部旧引用退出；业务结果只保留 meta.status／简短 text，无顶层业务 Target；部分清理或必要采用失败按 ERROR 写明已知效果及具体原因；原生冻结 12k／NEXT，补页不重做删除或清理 |
| manage_capability，AUTHORIZE_MCP | 仅 ROOT 原生、无 JS API；独立／Plugin 共用最小 MCP_CONNECTION target、action，删除模型 expected_*；完整复用现有 OAuth 登录、用户授权、任务复用、内部核对及结算，完成后才给最终结果；成功仅同一 Target、meta.status、简短 text；前端关联事实内部保留，原生冻结 12k／NEXT，补页不再次登录 |
| manage_capability，CLEAR_MCP_AUTHORIZATION | 仅 ROOT 原生、无 JS API；只填最小 MCP_CONNECTION target、action，内部核对由 runtime 取得；完整复用现有 logout，仅清本地 OAuth 记录并结算相关登录／刷新任务，不删除连接、不清非 OAuth 凭据、不承诺远端撤销；成功保留连接 Target、meta.status、简短 text，无记录正常清除仍成功；原生冻结 12k／NEXT |
| manage_capability，INSTALL_PLUGIN | 仅 ROOT 原生、无 JS API；source_path 统一为首位源目录 PATH target，保留必填 scope、可选 replace=false 与 source_format=native（native／claude／codex／cursor）；保留安装、替换及启用底层语义，新安装／替换均保持关闭；导入按组件 best effort，未支持项不启用，正常支持子集安装为 SUCCESS；返回最小稳定 PLUGIN Target、meta.status、简短 text，有用诊断入 content，内部安装身份不公开；实际部分清理或必要采用失败按 ERROR 说明已知效果；原生冻结 12k／NEXT，补页不重装或启用 |
| manage_capability，SET_PLUGIN_ENABLED | 仅 ROOT 原生、无 JS API；最小 PLUGIN target、action、必填 boolean enabled，内部安装条件由 runtime 核对；完整保留开启审阅（含重复 true）、关闭的组件退出范围及 Hook 信任，关闭不删包、配置、凭据或 OAuth；成功仅同一 Target、meta.status、简短 text，重复正常设置仍成功，不公开阶段枚举；已保存但必要采用失败按 ERROR 说明，原生冻结 12k／NEXT，补页不重新启停 |
| manage_capability，REMOVE_PLUGIN | 仅 ROOT 原生、无 JS API；最小 PLUGIN target、action，内部安装条件由 runtime 核对；完整保留实例移除、包 GC、数据及源目录保留、凭据／OAuth 和运行组件退出语义，不立即删包或清数据；业务结果仅 meta.status／简短 text，无顶层插件 Target；部分清理失败按 ERROR，已发生删除仍让 runtime 照常处理；content 直接说明已完成操作和失败原因；原生冻结 12k／NEXT，补页不重做删除或 GC |

`write_file` 的 tool_result_target 分支与 content 二选一，按完整 SESSION_ENTRY 结果 Target 导出全部保存内容，与模型已读页数无关；原生与 ROOT JS 共用参数、返回值和错误语义，详见[文件工具草案第 2 章](../native_tools/filesystem.md#2-write_file创建文本或导出完整工具结果)。`read_file` 的 NEXT 分支仅原生，唯一 target 参数原样传递运行时返回的 NEXT，不公开内部定位；每次交付原冻结结果的一页，tool_result 指向原完整结果，next 指原结果再下一页，详见[第 1.10 节](../native_tools/filesystem.md#110-next-target仅原生续读冻结结果)。JS 的 tools.read_file 只接受 PATH。

实施先按[有序内容统一存储 hardcut](PULSARA_ENTRY_READABLE_CONTENT_STORAGE_HARD_CUT_DRAFT.zh.md#10-数据库优先与停用期间的分步实施)落实数据库结构与存储基础，允许停用 Pulsara 后先重建新库，再按已确定的各工具合同逐步接通并验收消费者。已接通路径的聚焦验证记录与完整产品验收状态见实施计划。

本文件与[统一工具结果交付与分页草案](PULSARA_UNIFIED_TOOL_RESULT_DELIVERY_AND_PAGINATION_HARD_CUT_DRAFT.zh.md)配合使用：该草案定义完整保存、原生 12k 分页、JS 完整返回及 Cell 图文 steer；本文件细化工具本身的合同。这里已明确的工具参数、字段、meta/content 形状及状态表达，以本文件为准。

文中的原生调用与原生消息使用纯文本示意；JavaScript 代码块只表示 Code Mode 脚本，TypeScript 代码块只表示拟议 SDK 类型。示例中的尖括号表示说明占位，不是可直接使用的 Target 或观测值。

## 2. 本轮确定的结果表达

### 2.1 业务结果与原生交付

工具的完整业务值使用以下分工：

| 位置 | 职责 |
| --- | --- |
| target | 实际读取或操作的业务对象；文件工具为解析后的文件，find_files 为搜索目录，search_file_content 为搜索文件或目录，exec_process 和 manage_process 的正常进程结果为 PROCESS Target，exit_plan 方案文件实际保存成功时为该文件的 PATH Target；manage_capability 的 INSTALL_LOOSE_SKILL 成功时为安装后的 SKILL Target |
| meta | 本次调用状态及工具必要的类型化业务信息 |
| content | 最直接的可读内容，使用有序内容项对象数组 |

query_processes 的进程 Target 只放在 content 的每条 object 记录中，单查与列表都不在顶层重复 target。

不再将业务 JSON 序列化为字符串塞入 `body`，也不再叠加 `pulsara_tool_result` 外壳。meta 中的业务字段保留各自类型，不能把它们全部变成需要脚本重新解析的说明文字。

原生工具消息从同一业务结果投影。正文、业务对象 Target 与 meta 直接呈现；原生页面另外提供完整结果的 `tool_result` Target 和未读内容的 `next`。`tool_result` 用于导出完整结果；`next` 仅用于结果分页。它们不混入 JS 的完整业务值，也不作为 Cell steer 的输出包装。

现有观察信息在原生消息顶层并列呈现：`observed_at_utc` 是实际观察时间，`observation_duration_microseconds` 是实际观察耗时，单位为微秒。二者与 target、meta、content、tool_result、next 同层，不放进 meta、content 或嵌套 observation。未测得耗时不得伪造为 0，仍沿既有观察 owner 表达实际可用事实，不为本次修订增加一套计时机制。

JS 的 `tools.read_file`、`tools.write_file`、`tools.edit_file`、`tools.find_files`、`tools.search_file_content`、`tools.web_search`、`tools.web_fetch`、`tools.exec_process`、`tools.manage_process`、`tools.query_processes`、`tools.query_agents`、`tools.send_agent_message`、`tools.stop_agent`、`tools.run_agent`、`tools.wait_agent`、`tools.decide`、`tools.search_sessions`、`tools.search_session_content`、`tools.read_session_content`、`tools.view_image`、`tools.read_video_frames`、`tools.read_transcript`、`tools.call_mcp_tool`、`tools.read_mcp_resource`，以及 `tools.query_capabilities` 的 SKILL、HOOK、MCP、PLUGIN 四个分支，成功时直接返回各节定义的完整业务值，不套用原生消息的时间外壳、12k 页预算、tool_result 或 next。数据库内容、JS 值和原生投影来自同一份结果，不保存第二份手工维护的业务正文。

content 的每项统一为 type/value：文字项的 value 是字符串，图片项的 value 是 IMAGE_REF Target，Target 项的 value 是该工具声明的具体 Target，object 项的 value 是 JSON 对象。find_files 将命中文件保存为 PATH Target 内容项，不并列返回 targets 或重复文字清单；search_file_content 的匹配行和计数使用 object 项；PLUGIN 精确查询使用 text 与 SKILL／HOOK_SOURCE／MCP_CONNECTION Target 项交错返回。原生工具消息及显式 console.log／return 将 Target 项投影为文字引用，将 object 项投影为通用 JSON 文字，不自动打开、读取或加载其中的文件；图片项按当前模型能力投递真实图片或固定文字说明，原生能力不足显示 ERROR，Cell steer 不增加工具状态外壳。console.log 每个参数及显式 return 均递归识别实际输出任意层级的 image 内容项，其他内容呈现一次；不解析引用字符串或扫描未输出变量。普通文字项仍不自动增加分隔符。共同类型见第 2.4 节，投影及准入沿统一交付草案。

这些 PATH 工具不公开文件修订标记，也不接受模型提供的修订条件。读取、创建及结果交付不产生后续编辑资格，不维护已读行区间或通过结果窗口回填资格。edit_file 不要求模型先调用 read_file 或通过特定入口读过全文；文件路径权限与真实交付范围仍各自保持原义。

### 2.2 状态只表达一次

公开调用状态只使用 `meta.status`，枚举固定为 SUCCESS、ERROR、CANCELLED。不再并列输出外层 result_state、顶层 status、正文里的 status:ok 或另一组错误分类字段。

| 状态 | 含义 |
| --- | --- |
| SUCCESS | 本次工具调用成功；不表示模型已看完全部结果 |
| ERROR | 本次调用失败；具体原因、涉及路径及有用提示放在 content |
| CANCELLED | 本次调用已取消，包含派发前取消；实际已发生事实仍如实说明 |

公开状态从真实调用结算结果产生：成功为 SUCCESS，取消为 CANCELLED，其余失败为 ERROR。参数校验、权限、业务效果与取消结算由各工具明确的合同及既有 owner 承担，不把内部分类重新作为公开别名携带。参数或运行时失败与已产生业务结果的失败仍按实际事实处理；没有业务结果时，不为了填满本表而伪造一个业务对象。

状态不兼任交付进度字段。原生页的 SUCCESS 可以伴随非空 next；正常交付完成时的 next:null 不改变原业务状态。业务、保存或交付发生错误时，保留已经确定的业务事实，不用新增多组状态字段代替说明。保存或交付失败沿统一交付草案处理，不重新执行业务。

exec_process、manage_process 的进程状态是独立业务事实：meta.status 表达工具调用是否成功，meta.process_status 与 exit_code 表达命令是否结束及怎样退出。正常启动、观察或完成控制动作并取得结果时，即使进程仍在运行或以非零退出码结束，工具调用均为 SUCCESS，JS 正常返回；不能因命令退出码非零而抛 ToolError。主动 kill 成功也为 SUCCESS，不将目标进程被终止等同于工具调用 CANCELLED。

read_file 的 PATH 分支、write_file、edit_file、find_files、search_file_content 的业务错误沿异常通道返回；在 `catch (err)` 中，err 是 JS 异常对象，`err.result` 是它携带的完整失败业务值，不是枚举或错误代码。确实存在该业务值时，也使用 target/meta/content 结构，meta.status 为 ERROR 或 CANCELLED，全部内容保留且不分页。两个搜索工具的失败业务值包含原因及实际已取得的文件或业务记录，不把它截成程序页。query_processes 同样沿异常通道提供实际存在的完整失败业务值，但按[第 3 节](../native_tools/terminal.md#3-query_processes进程清单与元数据)只使用 meta/content，不重复顶层 target。view_image 的实际失败业务值同样由 err.result 完整提供，具体形状见[多媒体工具草案](../native_tools/media.md#17-错误取消及交付失败)，不生成假图片项。参数或运行时失败若没有业务结果，就不伪造 err.result。不把旧的重复结果 ID、读取指令、可用性布尔值或内部错误分类带回新的公开包装。

read_video_frames、read_transcript 的实际失败业务值同样通过 err.result 完整提供，具体形状见[多媒体工具草案](../native_tools/media.md)第 2.7、3.6 节。转录失败保留已取得文字和真实未完成区间，不增加 partial 状态；原生 NEXT 只读冻结失败结果，不继续识别或重试 ASR。

### 2.3 错误说明与提示统一进入 content

query_capabilities 四个分支失败同样保留实际存在的完整 err.result，具体参数、来源观察及失败记录形状见[能力管理工具草案](../native_tools/capabilities.md)。call_mcp_tool 及 read_mcp_resource 的实际完整失败值同样进入 err.result；后者的目录分支可保留已取得的 object 记录，具体读取保留有序 text／image 及真实错误说明，详见[MCP 接入草案](../native_tools/mcp.md)。

错误展示使用相同的结果结构，不另造错误正文包装：

| 位置 | 职责 |
| --- | --- |
| meta.status | ERROR 或 CANCELLED，不再细分公开错误枚举 |
| content | 用文字项表达简短、具体的错误原因、涉及路径及有用提示；搜索工具还保留实际已取得的 Target 项或 object 记录 |

删除 meta.error，不增加替代它的公开错误代码字段。具体失败原因直接写入 content，例如说明文件不存在、参数无效、权限不足或执行异常，并写明实际涉及的路径；脚本不依赖另一套 FILE_NOT_FOUND 等公开代码枚举判断分支。文件原文或其他工具真实业务数据中的同名字段不因此被扫描删除。

不再使用 `_hint` 字段，也不把它改名成另一个提示字段。原先 `_hint` 中有用的内容归入 content，可与错误说明合成一个文字项，或按顺序形成多个文字项；需要换行或分隔时由文字项自身提供。删除重复或没有实际帮助的劝告，不要求每次错误都附带操作教程。业务结果不再并列保存一份重复的 message；JS 异常自身的既有 message 仍按异常通道表达，不混作第二份业务正文。

本文 PATH 工具的失败业务值统一只保留 target、meta.status 和 content。文件属性只在各工具明确需要的成功值中作为类型化字段返回；失败时实际已知且有用的文件事实、已发生效果或不确定性写入 content，不在 meta 中保留可选文件属性，不伪造业务结果或已读范围。操作已经完成但保存或交付失败时，说明须保留已发生事实，不能统称为操作没有发生。

### 2.4 共同内容项类型

存储结果、JS 返回值、原生投影及显式 console.log／return 使用同一 type/value 内容项。type 决定如何交付，value 保留真实文字、类型化引用或 JSON 对象：

```typescript
type PathTarget = {
  kind: "PATH";
  path: string;
};

type ImageRefTarget = {
  kind: "IMAGE_REF";
  image_ref: string;
};

type JsonValue = string | number | boolean | null | JsonValue[] | JsonObject;
type JsonObject = {[key: string]: JsonValue};

interface TextPart {
  type: "text";
  value: string;
}

interface ImagePart {
  type: "image";
  value: ImageRefTarget;
}

interface TargetPart<TTarget = PathTarget> {
  type: "target";
  value: TTarget;
}

interface ObjectPart {
  type: "object";
  value: JsonObject;
}

type ResultPart<TTarget = PathTarget> = TextPart | ImagePart | TargetPart<TTarget> | ObjectPart;
```

各工具声明具体的 content 类型：文件正文、创建说明与 diff 使用 TextPart[]；find_files 使用文字项与 PATH Target 项；search_file_content 按模式声明 object 的精确 value 字段或 PATH Target 项；PLUGIN 精确查询使用文字项与组件 Target 项，value 精确限定为 SkillTarget、HookSourceTarget 或 McpConnectionTarget，类型见[能力管理工具草案](../native_tools/capabilities.md)。上面的默认 PathTarget 只供文件类用法，不把所有工具的引用都限制成路径，也不因此让任何工具接受任意种类 Target。object.value 的最外层是 JSON 对象，内部允许嵌套对象、数组及 JSON 基本值，不只声明为未知对象，也不新增 match 或 count 内容类型。输入参数中的 target、content 或 MCP 业务字段不因内容项统一而重命名。

Target 项展示引用，图片项按模型能力交付真实图片或明确的固定文字说明；导出只给图片引用，不声称已看图。object 的 value 整体按确定字段顺序、固定两空格缩进序列化为标准 JSON 文字，并带结束换行；保留花括号、方括号、引号和字段名，字符串及控制字符按 JSON 规则转义。原生和导出按这一文字规则处理；Cell 输出保留文字结构，并递归识别其中实际 image 内容项，在原位置交图或给失败说明。保存与 JS 返回仍为完整的原对象。

object 中的 target、targets 或更深层字段均作为普通 JSON 数据递归序列化，不拆出引用、不提升为 Target 项。裸图片引用不触发真实图片交付，不读取路径指向的文件；Cell 输出中的实际 image 内容项对象则不论嵌套层级均被识别，不根据字段名猜测媒体或工具操作。

object.value 本身是唯一权威业务对象，完整 JSON 文字按需投影，不再保存一份原始 JSON 备份或独立手写正文。原生按其文字投影的连续顺序进行 12k 分页，跨页片段明确属于正文片段，不冒充完整 JSON 对象；内部定位对模型透明，不生成对象字段游标或程序分片。JS 始终取得完整对象，console.log／return 沿既定 Cell 规则整条投影，不套用原生页预算。

## 3. 工具文档索引

各篇工具文档共同适用本文第 1、2 章的规则。

| 文档 | 工具章节 |
| --- | --- |
| [文件系统工具](../native_tools/filesystem.md) | 第 1–5 章：read_file、write_file、edit_file、find_files、search_file_content；第 1.10 节为仅原生 NEXT 续读、图文顺序及 object 跨页，JS read_file 只接受 PATH；第 2 章包含 content／tool_result_target 互斥输入、完整结果导出及原生／JS 示例 |
| [网页工具](../native_tools/web.md) | 第 1 章：web_search 六个参数、域名与日期条件、四家映射、精简返回值和原生／JS 示例；第 2 章：web_fetch 只传 URL Target，完整保存实际提取正文及可选标题，NEXT 不重新抓取；第 3 章：模型按需交错搜索与读取；第 4 章：默认 Firecrawl → Tavily 无 Key 免费池，可选四选一＋API Key、账户计费与静默回退，Exa／DeepSeek 不入免费池；只读／Plan 可用，ROOT／Subagent 原生、ROOT JS，共用权限与结算，已接入生产代码 |
| [终端工具](../native_tools/terminal.md) | 第 1–3 章：exec_process、manage_process、query_processes |
| [Agent 协作工具](../native_tools/agents.md) | 第 1 章：query_agents 三个查询分支；第 2 章：send_agent_message 交付语义及精简结果；二者支持 ROOT／Subagent 原生及 ROOT JS；第 3 章：stop_agent 参数、四个并列 Work 状态计数、逐项状态及原生／JS 示例，仅 ROOT；第 4 章：wait_message，Subagent 专用仅原生，超时 SUCCESS，正文独立交付；前四者沿冻结 12k／NEXT；第 5 章：tools.run_agent 参数、材料、同批引用与返回；第 6 章：tools.wait_agent 的 WORK／AGENT 双分支、完整快照、first／all、消息／超时／取消、返回类型及 JS 示例；后两者仅 ROOT JS、完整返回，无原生分页；第 7 章为实现参考 |
| [语义判断工具](../native_tools/decide.md) | 第 1 章：tools.decide 的两个参数、decisions object、available()、JS 示例、完整保存与错误语义；仅 ROOT JS、bypass 权限，无原生分页；第 2 章为实现参考 |
| [历史会话工具](../native_tools/session_history.md) | 第 1–3 章：search_sessions、search_session_content、read_session_content 的参数、返回类型及原生／JS 示例；单 ID 的 entry 定位、四种消息标签与正负 limit |
| [Plan 工具](../native_tools/plan.md) | 第 1–3 章：enter_plan、ask_plan_question、exit_plan 的参数、行为、返回值及原生示例；10 分钟交互期限、方案双份保存及顶层 PATH Target；仅原生，无 JS API |
| [记忆工具](../native_tools/memory.md) | 第 1–3 章：remember、query_memory、manage_memory 的参数、互斥分支、返回类型、原生示例及失败语义；runtime 自动取得并发条件；仅 ROOT 原生，无 JS API |
| [进度清单工具](../native_tools/todo.md) | 第 1 章：todo 的完整替换参数、10 项上限、meta.counts、返回类型、原生示例及错误语义；仅原生，无 JS API；第 2 章为生产实现参考 |
| [定时会话工具](../native_tools/schedules.md) | 第 1 章：共用 schedule 时间规则、创建默认值及完整规则类型；第 2 章：query_schedules；第 3–5 章：manage_schedule 的 create／switch、update、pause／resume／delete，含精简返回、原生示例、内部并发及队列语义；第 6 章为实现参考；仅 ROOT 原生，无 JS API |
| [展示工具](../native_tools/open_in_pulsara.md) | 第 1 章：open_in_pulsara 的参数、精简返回、原生示例及 ERROR 语义；合法展示项继续处理；第 2 章为实现参考；仅 ROOT 原生，无 JS API |
| [Code Mode 控制工具](../native_tools/code_mode.md) | 第 1 章：exec_cell 的 PATH target／code 互斥参数、简短启动结果及独立 steer；第 2 章：query_cells 的可选 target、列表与单查返回、空结果和错误语义；第 3 章：manage_cell 的 wait／stop 参数、pending_calls、精简返回及原生示例，保留已提交工具和各阶段 steer 的现行停止边界；均按冻结结果走 12k／NEXT；第 4 章为实现参考；仅 ROOT 原生，无 JS API |
| [多媒体工具](../native_tools/media.md) | 第 1 章：view_image；第 2 章：read_video_frames 五个参数，overview／detail／motion 的职责边界与问题驱动交错使用、最简索引与图文返回，cell_rect 沿现行角点坐标；布局与 motion 额外精确共享待实验；第 3 章：read_transcript 三个参数、逐段文字 object、原生／JS 示例及错误语义，删除累计 180 秒 ASR 限制和公开 media_version，完整取得保存后冻结 12k／NEXT；第 4 章：annotate_media 四个必填参数、仅原生、简短返回与错误语义，保留内部文件绑定，NEXT 不重复添加标注；第 5 章为实现参考 |
| [能力查询与刷新工具](../native_tools/capabilities.md) | 第 1–4 章：SKILL／HOOK／MCP／PLUGIN 查询及最小 Target，含原生／JS 示例和冻结 12k／NEXT；MCP 服务附已知资源／模板计数，未知省略、确认空才为 0，目录与读取交给 read_mcp_resource；第 5 章：reload_capabilities 无参数全量刷新、仅 ROOT 原生、精简返回及具体失败说明，部分失败为 ERROR；第 6 章为实现参考 |
| [能力变更工具](../native_tools/manage_capability.md) | 第 1 章：INSTALL_LOOSE_SKILL；第 2 章：SET_LOOSE_SKILL_ENABLED，保留开启与关闭；第 3 章：REMOVE_LOOSE_SKILL 删除范围、配置清理及部分失败 ERROR；第 4 章：TRUST_HOOK_SOURCE；第 5 章：REVOKE_HOOK_TRUST；第 6 章：SET_HOOK_SOURCE_ENABLED；六项均含参数、精简返回、原生示例、错误语义及冻结 12k／NEXT；第 7 章：MCP 的 ADD／UPDATE 共用 config，含字段、类型、新增默认、局部更新、映射替换及两类归属边界，保留局部更新中的用户配置，独立／Plugin 连接统一默认允许 ROOT 和子代理使用；第 8 章：ADD_LOCAL_MCP；第 9 章：UPDATE_MCP_CONNECTION，统一独立／Plugin 连接，含三项最简参数、两类修改边界、Plugin 恢复默认、编辑器分支、精简返回、错误及冻结分页；第 10 章：REMOVE_LOCAL_MCP，含最简参数、简短结果、原生示例、现有清理语义及部分清理失败 ERROR；第 11 章：AUTHORIZE_MCP；第 12 章：CLEAR_MCP_AUTHORIZATION，最小参数及精简结果共用独立／Plugin 连接，底层授权和前端语义保持；第 13 章：INSTALL_PLUGIN，统一源目录 PATH 参数、精简稳定 PLUGIN Target 结果，安装／替换及关闭状态保持现状，按 best effort 安装独立有效组件，未支持项不启用、局部失败不拖垮整包；第 14 章：SET_PLUGIN_ENABLED，最小参数、精简结果、原生示例和错误，保留开启审阅、组件退出及 Hook 信任；第 15 章：REMOVE_PLUGIN，保留现有包回收、数据、凭据及运行语义，部分失败按 ERROR，已发生删除仍正常处理；content 使用直白结果说明；第 16 章为实现参考；仅 ROOT 原生、无 JS API |
| [MCP 接入工具](../native_tools/mcp.md) | 第 1 章：call_mcp_tool 返回类型、正式 content 适配、原样 structuredContent、状态与派发事实；第 2 章：原生查询／准备 Cell 交付接口说明、同 session 引用、单工具变化及同目标重连复用；第 3 章：元工具参数和原生／JS 示例；第 4 章：read_mcp_resource 的连接目录／具体资源两分支、返回类型、原生／JS 示例、空结果及失败；第 5 章：冻结交付、发现刷新及实施核对；第 6 章为实现参考 |
