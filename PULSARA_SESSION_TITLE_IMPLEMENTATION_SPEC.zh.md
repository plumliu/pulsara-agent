# 会话标题修改

状态：已实施并验证；用户授权的一次性保留数据升级已完成，2026-09-30。

## 产品合同

- 侧栏会话「···」和当前会话顶部「···」提供相同的「重命名」入口，共享弹窗与保存接口。
- 输入预填当前显示标题并选中；Enter 保存（中文输入法合成期间不提交），Escape 取消；保存中禁止重复提交，失败保留输入，不伪报成功。
- 标题 trim 后非空、单行、无控制字符；没有额外任意长度上限，沿用 HTTP 请求物理大小边界。标题作为普通文本渲染。
- sessions.title 是唯一持久标题字段，允许 NULL 表示尚未自定义；显示时使用现有默认名称。不加入自动模型命名、历史、事件、回执或本地影子存储。
- 改名只更新 title，不修改 updated_at、writer、lifecycle、序列、消息、provider prefix、模型配置或归档时间。不需要停下运行中的会话。
- OPEN 和 ARCHIVED 都允许改名，归档/恢复后保留；已删除或跨 memory domain 返回不可用，不复活。更新与归档/删除由同一 session row 锁串行。并发改名按数据库最后提交值生效。
- 第一版归档页继续展示同一标题；重命名入口限定本次两个指定位置。fork 新会话保持默认命名，不冒用源标题。

## 实现边界

PostgreSQL sessions 新增可空 title；repository 执行仅该列的原子更新，Host Core 暴露不加载会话运行时的冷元数据操作，HTTP PUT /api/sessions/{id}/title 严格接受 {title:string}，返回 {session_id,title}。沿用本地 HTTP Host/Origin/会话域保护；不要求会话执行控制权，展示元数据不接入执行权限状态机。

前端 RuntimeAdapter 调用该接口，确认成功后更新既有 sessionList 中对应项，所有依赖该列表的标题视图同步。弹窗绑定目标 session ID，不因切换会话写错目标。已有列表刷新仍读取 canonical 标题。失败或响应不确定时保留输入、提示可刷新确认；不自动重试写入。

更新 clean-v0 baseline 和实际 PostgreSQL catalog 清单；不引入在线兼容分支。验证只用独立测试数据库，不自动重置用户保存会话的数据库。现有库部署方式必须明确，不能为激活此 UI 功能删除历史。用户本轮明确授权保留现有会话并执行一次性升级；这是对已使用本地库的一次维护例外，不增加生产在线迁移、旧新双读或兼容分支。

## 验证

数据库验证改名、归档、归档中改名、恢复、跨域拒绝、删除后不复活，以及除title外行值/消息不变。HTTP验证形状和缺失/跨域错误；前端验证两个入口共享交互、保存错误、中文合成、取消与标题同步；执行相关聚焦回归与前端构建。此功能不调用模型，无需真实provider dogfood。

仓库模块化门控登记 `rename_session` 为本功能新增的唯一 repository method 与数据库操作 owner。其余历史 owner 继续执行原有精确等价检查；不重写 checkpoint baseline。该新增 owner 的产品行为由上述真实 PostgreSQL 标题测试验证。

## 本轮验证记录

- 后端：`tests/test_session_title.py tests/test_session_archive.py tests/test_local_web_session_summary.py tests/test_local_web_session_order_postgres.py tests/test_stage5_clean_migration.py`，40 passed（5.48秒），既有 aiohttp shutdown_timeout 警告1条。
- 前端：`components/session-rename-dialog.test.tsx components/workbench-view.test.tsx app/pulsara-app.test.tsx lib/runtime-adapter.test.ts`，275 passed（13.26秒）。之后新增旧列表响应覆盖标题的回归单测1项通过；不把不同运行批次混写为一次结果。
- 前端改动 ESLint 通过。
- TypeScript类型检查和build:local通过，产物已更新；现有打包体积提示保持。Python改动Ruff通过。
- catalog取自独立空数据库实际执行新baseline后的检查结果，无手工拼接假catalog。
- 初始只读检查：当前保存的本地 pulsara 库有1条会话，缺少title列。用户明确授权保留数据升级后，先在独立旧基线库演练，再核对真实库旧catalog与ledger，事务内添加title列并同步新基线ledger。
- 真实库升级后：1条会话、50条transcript记录保留，所有产品表记录数相同；session原有字段逐项相等。实际catalog等于新clean-v0，普通生产schema verification通过。保存配置未改动，没有reset、DROP或删除历史。
- 只保留本次升级事实记录，不保留一次性临时升级脚本作为生产兼容入口。运行中的旧服务需重启加载新接口。
