# Phase B 真实 provider：terminal / 输入关闭 / Linux工具链与Windows PE

2026-10-25。用户在真实Windows GUI保存配置后授权执行。使用正式be6af安装候选的Linux standalone Python及已装pulsara_agent，不使用共享clone旧源码。只读 `LocalSettingsStore(require_pulsara_home()/local-settings.yaml)`，唯一当时有效连接 `connection:oeeiipbn63ytbexaxw74nv5tgy` / `deepseek-flash` / `openai_chat_completions` / route deepseek / catalog_standard；原runtime解析默认effort=high。未打印/复制key，未写保存配置，没有环境key fallback。

## 执行边界

复用原 `run_terminal_cognitive_dogfood.run`、KernelHostCore/ModelRuntime与 `_RecordingModelRuntime`。output-only适配只负责测试factories、准确Unix socket目标、短私有应用home和测试提示词；生产owner、scope/SDK、provider transport、tool面、SYSTEM及协议没有改动。每批新冷session；批内只追加输入。原runner的`prompts`字段保留其原测试文本，closure/toolchain变体的真实输入以`actual_prompts`及完整`provider_calls.provider_input`为准，不将别名case名当场景证据。

固定正式PG17.9 system_identifier=7700523439330484737，data=`/home/plumwsl/.pulsara/desktop/postgres`，socket=`/home/plumwsl/.pulsara/desktop/socket`，roles pulsara_admin/pulsara_runtime，无TCP。每批先重核准确实例，再创建唯一UUID测试DB、原clean-v0/migration，结束只drop该批DB。没有启停/reset正式PG，不触GUI新session/canonical用户行。

interop使用唯一configure_interop，固定Ubuntu/plumwsl和已安装PE helper：`/mnt/c/Users/plumlocal/AppData/Local/Programs/pulsara/resources/windows-interop/pulsara-interop.exe`。原scope持有Linux/Windows物理生命周期；没有monkeypatch其join或用Linux PGID代替Windows Job。

## 实际结果

| 记录 | 真实provider calls | 结果 |
| --- | --- | --- |
| terminal-real-provider.json | 22 | 首轮失败：测试应用home错误地跟长工程前缀，实际Unix控制路径超界；原结果保留 |
| terminal-short-home-real-provider.json | 38 | 原9个有限case完成，实际tool/transcript另经review脚本核对 |
| terminal-input-closure-real-provider.json | 7 | PIPE实际EOF/exit0，但模型先送不合法yield字段，严格测试失败；未进PTY，保留 |
| terminal-input-closure-clarified-real-provider.json | 10 | 仅澄清测试输入schema；原严格断言未放宽，PIPE/PTY两项通过 |
| terminal-toolchain-real-provider.json | 2 | venv/Git成功，裸node不在此工作区PATH，自然127，PE未执行；保留 |
| terminal-toolchain-explicit-node-real-provider.json | 2 | 指定现成Linux Node绝对路径，venv/Git/Node/Windows PE在同一原terminal任务自然exit0 |

原9case的实际事实：

- 中文空格工程UTF8路径99字节；alpha→beta→alpha三个独立命令读到准确标记。同一次命令export=inside可读，下一独立命令回workspace且变量为空。PIPE默认False由当前tool schema定义，原命令行为保留。
- write无换行hello加submit world得到`ANSWER:helloworld`；显式PTY得到计算输出`ANSWER:delayed`，与输入echo区分。quit自然exit0。
- 后台1.2秒命令返回running后等待同process得到LONG_DONE/0；截断命令仅执行一次，artifact_read补到MIDDLE_7319，无重做。
- 快速完成与2秒monitor在注册时已结束，原owner明确PROCESS_ALREADY_TERMINAL，模型读取同process终态；不假造它们实际注册成功。
- 120秒completion-only monitor实际注册后结束初始turn；无手工followup/poll，自主唤醒模型报告MONITOR_LONG_DONE/0。原严格run断言验证同process/monitor标识、真实COMPLETION和elapsed>=120。
- 8个sleep120真实running；第9条拒绝PROCESS_CAPACITY_EXHAUSTED且process_id=null；原8个准确ID逐个kill，全部killed/-15，无替代启动。review另核8个ID集合与9次启动请求及精确kill。

补项的严格事实：

- PIPE tty=false与PTY tty=true，各仅启动一次，submit实际数据后只一次close_stdin，不发送quit/手工Ctrl-D、不kill/重启；同process最终EOF_SEEN、success/exit0。前一次模型多送yield_time_ms被原schema以INVALID_ARGUMENTS拒绝（未提交物理关闭），改正后PIPE成功；严格测试仍整体失败。后续只在新冷session提示精确keys，未修改schema或降低断言，成功批每条trace均SUCCESS。
- `.venv`由正式Linux Python `-m venv --without-pip`创建在该中文工程，实际sys.platform=linux、sys.prefix!=base_prefix、解释器位于工程.venv；Git2.53.0真实init并rev-parse=true。
- 首轮裸node未找到，此用户独立工作区的PATH事实不抹去。成功补测明确使用已安装Linux ELF `/home/plumwsl/.local/share/mise/installs/node/22.23.3/bin/node`，平台linux/version22.23.3，实际写读node-check.txt标记。没有修改PATH/装新依赖，不据此宣称裸node默认可用。
- 同一个原terminal命令实际调用 `/mnt/c/Windows/System32/cmd.exe /d /c echo PULSARA_PE_DONE`，观察标记并自然exit0，未kill。cmd报告UNC cwd不支持而使用Windows默认目录；echo无外部文件写入，此结果不代表任意Windows程序都能保持Linux工程cwd。

## 保留的harness边界和故障

首轮testhome94 UTF8字节；scope的`/desktop/i-XXXXXXXX/c`为115字节，超Linux108字节协议边界；随机目录已原cleanup退休，shape长度可准确复算。如该home用于正式managed PG，`.s.PGSQL.5432`路径123字节也超过原PG104边界。正式默认home20字节，control43/PGsocket51。该测试用独立DB绕过原PGhome长度检查，却把超界长前缀同时用作home；不以此推断正常默认home有新生产缺陷。output adapter随后只将PULSARA_HOME分配到短私有ext4目录，99字节中文workspace不变。没有移动生产scope存储域或缩短工程以掩盖能力。

失败批自行到原strict monitor断言后结束；尝试精确SIGINT检查时已无该harness PID，因此未实际发送取消信号。各原失败JSON完整保留，不把报告中“Completed case”字样当作通过。没有重写首次失败为成功，也没有复制旧会话/provider输入。

## 输入连续性、秘密检查与清理

`provider-records-review.json` 对六批完整记录分别做精确值比较：各自批内SYSTEM一致、tools完整相等、messages前缀相等且仅追加；保留实际prompt/响应/normalized blocks/tool结果，未生成新DTO/hash/registry。六批记录均通过原settings的credential_scrub_set实际秘密值contains检查，configured_secret_found=false。不宽泛遮盖模型答复、错误或工具正文；原模型错误和UNC警告照常保留。

六个独立UUID DB均由原finally drop，后续只读pg_database逐一确认不存在；正式systemid仍同一值。成功批在原core.shutdown后清其短home和长工程；失败批保留现场至复核后精确清理。两次private tests/harness根 `.provider-accept-xqgw_1ur`、`.provider-accept-qbf27gnx` 已删除，保留失败的短home也核无scope目录后删除。cleanup只匹配确切测试argv或PULSARA_HOME，不处理其他进程；可读范围未发现测试进程，有两个同UID PID355/357的/proc环境不可读，报告该限制，不声称全权限扫描证明所有Windows进程绝对不存在。原owner正常关闭/实际工具终态、scope目录退休及DB不存在是本批清理证据。

`first-batch-cleanup-result.json` 与最终`cleanup-result.json`分别保留清理事实；正式PG/保存设置/GUI工作区未由测试停用、重置或删除。

## 资格范围

本报告支持§13.3所列terminal、PIPE/PTY输入关闭、monitor/容量、Linux venv/Python/Git/Node及一个无文件副作用Windows PE的真实模型有限证据。它不替代MCP stdio/HTTP/hooks、subagent/compaction、文件/图像/导出、关闭后history/memory及真实完整Electron Quit重开；这些由root和其余分工分别补验。不宣称Windows发布或B5全部完成。

后续已独立补齐MCP stdio/HTTP与PostToolUse hook的模型实际执行正控，见[专项报告](mcp-hook-execution/report.zh.md)。该项未与本表合并计数，原失败亦保留。

重现脚本保留工作目录的明确路径和已安装目标约束；原private目录已退休，必须重新prepare并重新核准当前systemid/保存连接，以新输出文件执行。archive中的脚本是当时源码留档，不应把旧prepared-target当执行权限或自动重放任务。
