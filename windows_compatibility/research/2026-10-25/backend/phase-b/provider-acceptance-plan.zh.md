# 真实 provider 验收准备（仅方案，尚未执行）

2026-10-25。已读当前 `tests/dogfood/run_terminal_cognitive_dogfood.py`、其 model-switch DB helper、artifact/subagent runner 入口、当前 interop 配置及规格§13.3。用户现在没有保存模型配置；等待界面可用后用户亲自配置，不要求在聊天或 shell 提交 key。

## root 需要准备的非秘密输入

1. 实际已安装 Windows 候选版本与固定 WSL distribution/user/UID/home、安装的 guest Python/资源/Windows interop helper 路径。确认应用 cold start 的可信 host 帧已安装 `configure_interop`，不能仅因处于 WSL 就认为 Windows Job 路径启用。
2. 用户在现设置页保存并测试可用的 model connection；记录 connection_id、可用 model_id、wire API、支持的 reasoning effort（若有），不用 runner 默认模型猜测连接。不复制/导出凭据。
3. 用户保存配置所属有效 Linux Pulsara home 的路径。通过该 home 的 LocalSettingsStore 一次读取后作只读内存快照，切换测试 home 前完成读取。不用环境 key fallback，不写回此 store。
4. 已明确核准可丢弃的独立本地 managed PG：固定17.9、native ext4、实际 data/socket、UID、SQL system_identifier/current_database/current_user、无 TCP；runtime/admin DSN 可记报告，但不把任意保存目标当可重置数据库。由原 TS BundledPostgres 和 helper 启停，不另起 PG 生命周期 owner。
5. 当前用户实际 home/workspaces 下唯一中文空格工程、输入样例（文本/图片/HTML）、输出目录及 Linux Python/Git/Node 和 Windows 测试 PE。MCP stdio/HTTP 与 hook 使用无秘密、可控的本地 fixture；不借真实业务服务填生命周期测试。

## 当前 runner 的真实接缝

terminal runner 先读保存配置，再 `_create_database(saved)`，然后临时 home + `_ReadOnlySettingsStore(replace(saved, postgres=...))`，构建原 `ModelRuntime.production` 和 `KernelHostCore.production`。它已经沿原生产 owner 执行，且 finally close_session/shutdown/drop。但其 DB helper 明确只允许 host=localhost/127.0.0.1/::1 且 dbname=pulsara，不能直接接受本次 Unix socket。不要开 PG TCP 或改保存 settings 迁就它。

准备执行时最小测试 harness 修正：对已核准独立 Unix socket PG 的精确 data/systemid/socket/role 做只读核对，再仅为本次 runner 创建唯一 DB、由原 migration owner clean-v0；把该目标只注入原只读 settings 快照，finally 只删本次唯一 DB。复用同一 `_create_database/_drop_database` 测试边界，不另造 runtime/数据库 registry，不扩大到任意远程目标。实际落代码前需再核本次安装资源和目标，不在本方案内执行。

terminal runner 本身不调用 `configure_interop`，独立进程也不会继承 desktop_runtime 的进程内配置。正式 owner 定向 runner 应在原 KernelHostCore 构造前，使用同一已核准安装 target/helper 调用该唯一配置函数一次；不 monkeypatch Windows scope，不自行设置 WSL_INTEROP。它只能证明 owner+provider 路径，不能替代 Windows Electron 启动/关闭的证据。

实际 Electron 长程验收优先复用已安装应用、原 desktop_runtime 与原 HTTP/session owners，通过界面/现有 API 操作。若必须严格使用隔离 home 和只读保存快照，已有 `run_desktop_child(..., application_factory=...)` 与 `DesktopApplication(settings=..., model_runtime=...)` 是测试注入点；正常 CLI 不提供跨进程任意 model/settings 注入。不可声称启动当前普通 CLI 就自动完成隔离注入，也不要为测试新增生产 secret 帧。可用一个测试入口调用原 run_desktop_child 和原 DesktopApplication factory 注入；这仍应单独注明是 harness，不冒称安装版完整链。root 可选择安装版在已核准可丢弃的新用户数据环境中现场配置，避免这一步注入需求。

## 最小执行批次与资格

| 批次 | 复用入口/动作 | 必须留下的事实 |
| --- | --- | --- |
| 先导 | terminal runner `--phase candidate --model <保存可用ID> --cases directories,interaction,pty,long,truncation,monitor_race,monitor,monitor_long_wake,capacity`，使用新的独立输出文件 | 原 prompt/reply/tool结果；PIPE/PTY 输入、running继续工作、截断补读不重跑、monitor同process唤醒、8槽和精确取消；120秒是已有实验case，不是产品寿命上限 |
| Windows 跨域 | 同一真实 session 调用已安装 Windows PE 正常退出/后代尾部/取消，分别从 terminal、hook、MCP stdio 原 owner 进入 | Windows Job/scope 和 Linux group/reader 都 join 才完成/释放；普通完成不用强杀；scope 已启用的真实观测，不用 Linux-only通过替代 |
| subagent+compaction | `run_round10_subagent_dogfood.py --connection-id <ID> --scenario all --output-dir <唯一目录>`（先修同一 DB前置并安装interop）；其现有 capacity 场景已含 MCP/compaction | 原 scope/消息/输入/容量/compaction successor，SYSTEM/tools/messages 按原 epoch合同；正常生产全tool面，不删工具减少难度 |
| 文件与显示 | 已安装 Electron 在中文空格工程中操作 Linux venv/Python/Git/Node；导入、HTML/image预览、导出；artifact runner仅 `--phase candidate` 作定向补证 | 原文件owner与Windows交互实际往返；artifact baseline会删tool，禁止用它作本次before。before/after只是输出目录名 |
| 退出/重开/检索 | 实际应用正常 Quit 后重开；独立已核准目标做异常退出后显式重开；通过原历史/memory查询owner验证原canonical记录 | kernel真实aclose/PG停止次序、未知保data、PG自身恢复、不自动重放；进程本地观察丢失不冒充持久恢复；可复用 direct-memory/history runner补语义但不能替代GUI生命周期 |

先导通过才能扩入其余批次；任何失败保留原始输出和确切过程，不自动重新跑副作用。上述每行不能互相替代。只跑 terminal runner 不足以覆盖§13.3的MCP HTTP、hook、subagent/compaction、图像文件和关闭后历史/memory，也不满足 Windows 激活。具体提示词沿现 runner，必要GUI步骤写独立有限case，不增加总轮数/任务寿命限制。

## 证据与收尾

保持 prompt、完整 provider 输入 SYSTEM/tools/messages、provider响应、tool结果及 process/thread/FD/scope 状态；现 terminal runner保存prompt/reply/tooltrace，**没有完整provider wire-input记录器**，完整prefix证据需复用现 recording adapter（model-switch等已有），在原 ModelRuntime 的支持注入点记录，不重写provider transport。凭据过滤用 `saved.credential_scrub_set()` 的实际秘密值集合；当前 terminal `save` 只收 model_api_keys，扩入 MCP/hook 后必须覆盖实际全部配置秘密，不能只用key子集。精确清除秘密，保留其它正文和错误。

先结束所有session/原core/application，再核Windows scope/Linux组/reader实际join和kernel锁，最后由原TSowner停止确切PG；只清本次home/workspace/DB，保存配置保持原样。不要在失败finally清空所有用户process/registry或终止发行版。当前未开始以上任何provider网络调用，也未读取/输出保存凭据。
