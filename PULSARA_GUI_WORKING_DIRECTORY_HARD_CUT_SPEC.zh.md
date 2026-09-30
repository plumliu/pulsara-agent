# GUI 对话入口与应用启动工作目录

日期：2026-09-30。状态：目录分离与 Host CLI 删除已实现并验证。

`pulsara app` 只启动应用。新建会话的工作目录由 GUI 的指定目录选择，或由快速开始的既有目录创建 owner 决定；恢复已有会话使用其 canonical 目录。启动进程的 cwd 不得成为会话目录、目录预填值、项目能力扫描根或 bootstrap 中的项目身份。

完整删除 `app` 的 `--workspace`、`--workspace-kind`、`--transient-display-label` 与 Web owner 的启动 `HostWorkspaceInput`。不保留别名或 cwd fallback。显式作用域的能力管理 CLI 保持各自合同。

对话的产品入口统一为 GUI。完整删除 `host` CLI（包括 `run`、`repl`、恢复/继续/列举会话参数和 REPL 交互指令）、专用启动与输出 helper、`pulsara_agent.repl` 模块及其测试、直接 `prompt-toolkit` 依赖，并更新依赖锁文件、README 与现行架构断言。不保留 alias、废弃 wrapper 或新的 headless 入口。`KernelHostCore`、GUI 会话管理、renderer-neutral 协议与模型内置 terminal 工具继续使用原 owner 和生命周期；内部测试/dogfood 可直接使用 Kernel API。历史合同留在 archived_docs，不恢复其旧产品入口。

Web application/controller 只携带应用级 memory domain 和已有权限、Skill、workspace MCP 信任偏好，不持有项目根。bootstrap 不再发布 `workspace`；前端从真实 session summary 读取工作目录。新建会话对话框未选目录时为空，指定目录必须经过 GUI 选择；原生选择器可以从当前 GUI 会话目录开始，未有会话时沿用选择器的用户主目录起点，起点不等于选择结果。

用户 Skill 管理复用 `LooseSkillDefinitionProducer`，在未指定项目时只观察两个用户根。完整会话观察仍使用原四根、优先级、资源限制和完整性规则。不得用 cwd、Pulsara home 或伪项目补出两个项目根。

无会话的用户 MCP 连接测试复用现有 MCP 管理与 SDK 生命周期，其管理操作目录明确为 `require_pulsara_home()`，只用于该测试的相对 stdio cwd；有会话的项目测试仍使用该会话目录。此管理操作不建立项目或会话，不将管理目录发布为用户工作目录。实际会话内的 MCP 执行继续由该会话 owner 决定目录。

不改变会话创建时机、canonical schema/clean-v0、事件/subject/guard/relation/job 数量、provider epoch 或 prefix 合同，不新增恢复机制、指纹和总量上限。旧进程的配置不在执行中替换；新入口在应用正常重启后生效。

验证：CLI 在解析时拒绝 `host` 及已删除的 app 参数，帮助只提供应用启动和现有管理入口，`app` 正常调用 Web owner；从含项目能力的任意 cwd 启动 bootstrap 无 workspace；用户能力扫描不访问 cwd 项目根；GUI 指定目录未选择时不能提交，选择后创建 exact 目录，快速开始/已有会话保持原行为；聚焦覆盖 CLI、架构约束、Web 生命周期、会话恢复、memory、能力管理与前端适配器。使用仓库 `.venv`/uv，保存的生产配置只读，不要求新的 real-provider 调用或 dogfood 证据。

目录分离完成验证：后端相关测试 289 项、前端相关测试 218 项通过；Python 静态错误检查与前端构建通过。全局 editable 启动器从 `/tmp` 正常启动，实际 bootstrap 无 workspace；GUI 指定目录初始为空、未选择时禁止提交，已有会话仍显示其 canonical 工作目录。

Host CLI 删除完成验证：CLI、控制语义、架构、Web 与 Skill/MCP/Hook/Plugin 相关测试 179 项通过；静态检查与 `uv lock --check` 通过。仓库 `.venv` 和已安装的全局 editable tool 已同步移除 `prompt-toolkit`、`wcwidth`。全局 `host run`/`host repl` 在解析阶段以 exit 2 拒绝；全局 `app` 从 `/tmp` 正常启动，bootstrap 的应用与数据库均 ready。临时验证服务已正常停止。

扩展仓库模块化门控已同步既有功能：按会话标题规范登记 `rename_session` method 与数据库操作 owner；按工具续行规范将三个 Round 5 旧参数化节点和一个 provider failure 旧节点对应到全部八项替代测试，并强制要求替代节点被收集。专用 REPL 的五项测试登记退役，GUI 架构节点名同步更新。checkpoint fixture 保持原历史真值。模块化架构测试 5 项、会话标题/搜索/provider stream 测试 36 项、写工具异常续行/取消的真实 PostgreSQL 测试 6 项全部通过。
