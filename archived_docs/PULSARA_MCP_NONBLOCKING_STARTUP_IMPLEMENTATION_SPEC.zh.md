# Pulsara MCP 非阻塞启动与不完整搜索合同

> 已归档。当前工具参数和用法见 [native_tools/](../native_tools/)。

状态：已完成实施并归档。与 [MCP 初始目录规范](PULSARA_MCP_CATALOG_INCREMENTAL_IMPLEMENTATION_SPEC.zh.md)共同定义非阻塞首请求、轻量服务目录和不完整搜索；前缀、调用及结算合同保持。本轮不构建、安装或操作运行中的应用，不修改生产设置或生产数据库。

## 1. 启动与所有权

ROOT、恢复会话、Subagent、定时任务和其他入口的实际首请求均不等待 MCP 握手或目录发现。初始目录从当前有效作用域的 enabled 配置生成，允许连接尚未就绪。provider 只安装固定 Pulsara 工具；远端 schema 仍按需查询。

删除 runner → provider dispatch → tool port → supervisor 的全局 cold readiness 路径，以及只服务该等待的 observer、唤醒事件、配置采用回调注入和专属等待界面。前端不再通过 session ephemeral 读取 mcp_readiness，也不显示阻塞式“正在连接 MCP”及其逐服务等待/失败摘要。通用运行状态、停止按钮、title/suggestion 观察和用户级能力页连接状态保留。

既有 Host 配置采用 safe point、requested/applied revision、必要失败传播保持；不因删除等待吞掉配置冻结错误。supervisor 继续异步启动、发现、刷新、重连、重试和物理清理。既有 typed connection-task outcome 同时服务 live inspection，保留它以区分“语义目录仍可保留”和“物理重连已失败”；不得把失败显示成 CONNECTING。

不新增另一处等待、自动查询、自动重试、后台作业或持久状态。成功发现的待采用结果仍经既有 safe point 接纳，查询/调用读取现有 owner，不修改同 epoch 的 SYSTEM、tools 或已安装消息前缀。

## 2. 搜索覆盖与返回

query_capabilities 的 MCP 搜索在本次 scope、parent、ROOT/Subagent 及公开 Target 范围内判断覆盖。已禁用服务和范围外服务不导致不完整。公开连接按 config_path + server_id 区分，先把记录与当前配置的实际来源相接；同名 USER／WORKSPACE 或 Plugin 连接不能借用另一来源的已接纳目录、schema 或引用。已启用但尚无对应当前配置的已接纳完整工具目录时，该范围的搜索不完整，包括连接中、发现中、初次失败或配置替换后尚未发现。

“完整”指当前配置对应的已接纳目录，不承诺远端此刻没有更新。相同语义重连时仍保留的已接纳目录可以搜索；物理 lease 不可用影响 invocation／真实调用准入，不单凭连接重建抹掉已知工具事实。配置已改变的旧目录不得被当作当前目录已完整发现。

| 本次事实 | 返回 |
| --- | --- |
| 范围完整且有命中 | SUCCESS，现有工具/服务记录 |
| 范围完整且零命中 | SUCCESS，明确无匹配 |
| 范围不完整且有命中 | ERROR，保留全部已知匹配及可用 schema/ref；text 说明哪些服务目录未取得，结果不是完整搜索 |
| 范围不完整且零命中 | ERROR，说明在已知目录未找到匹配，列出未完成服务，不能确认不存在合适工具 |
| 来源读取失败 | 既有 ERROR，保留已知观察及具体失败，不附全局无匹配断言 |

说明只用既有 meta.status／content text，不新增 complete、pending、retry_after、状态枚举或搜索注册表。服务名使用公开连接 Target server_id 和已有轻量 Plugin 来源。查询仍不等待远端、不发额外 listing，不自动循环重查。

不完整查询的已知有效引用继续随完整结果接受而生效；native ERROR 和 JS err.result 保留这些记录。公开 ERROR 映射到既有内部 APPLICATION_ERROR 执行态后按原路径保存和接纳；SUCCESS／CANCELLED 保持各自的执行态及公开语义。ERROR 不撤销已知事实，也不自动要求重做查询。用户随后可调用已知合适接口、再次按需查询或采用其他授权方式。

默认 ROOT／Subagent 提示词只用自然语言说明 ERROR 可含已发生效果和可用部分结果，应区分成功、失败和未知部分，不默认全部失败或盲目整批重试，失败或取消不证明回滚。ROOT 仅保留 Code Mode 的入口、权限和先读 bundled Skill 的高层指引；具体 JS 返回、输出、Agent 参数及 Decision 技法由现有 bundled Skill 负责，Subagent 提示词不包含 Code Mode／JS 内容。

## 3. 验证与交付

- 控制 initialize/discovery 未完成，证明 ROOT、恢复 ROOT、恢复/新 Subagent、scheduled 首请求已经发出，目录出现但没有 direct 工具。
- 证明后续发现/查询/meta 调用成功且前缀 append-only；普通停止不取消后台连接，Host close 仍完整收尾。
- 覆盖零/非零命中、不完整/成功空目录、disabled、ROOT_ONLY、scope/parent、Plugin 同名、同语义重连和配置更换。
- 证明部分 ERROR 接受后的有效 REF 可实际调用，JS err.result 可取得同一结果。
- 原 SDK 传输、导入线程隔离、权限、配置采用和取消/效果结算回归保持。
- 旧测试若只用全局 wait 准备 fixture，改用 tests-only 捕获实际连接 task 的辅助等待；生产不保留旧 API 或别名。
- 保存的 DeepSeek Chat Completions/Responses 使用隔离 home、只读设置注入和独立数据库验证首请求不等阻塞服务，并记录真实 prompt、输出、查询及 RPC；只脱敏实际凭据。

报告与实验位于 output/hardcut-20261116/mcp-nonblocking-startup/。
