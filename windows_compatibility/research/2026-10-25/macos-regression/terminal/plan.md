# a57ce3b1 macOS terminal / MCP / hooks 回归计划

2026-10-25，terminal专题，只读计划；尚未开始测试，等待root确认统一uv环境。已读AGENTS、当前`windows_compatibility/PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md` §24、生产manager/environment/output/monitor/hooks/MCP stdio、Windows正式接入报告与关闭链审阅。旧根Windows spec已搬入windows_compatibility，历史A/interopoff不是当前执行合同。

当前HEAD `a57ce3b103214d3a0865e0460c0f7dd0d0aa97a4`；任务开始git status为空。全部命令/log/probe只写本目录。禁止mise/uv sync/npm ci/build、用户正在运行的/Applications app/PG、用户CLI/settings与系统PG。真实OS sleep/wake无人协助，留待验收，不模拟成实测。

## 首组：无PG、真实POSIX与原业务合同

环境ready后用repo根`.venv/bin/python -m pytest`，不启动provider，不读保存配置，独立temporary PULSARA_HOME由root/本实验明确注入。拟文件：

- `tests/test_terminal_startup_cleanup.py`：新foreground timer/Popen/fdopen/reader/state创建失败7个真实OS清理反例。
- `tests/test_terminal_output_settlement.py`：retention callback尚未结束时revision observation必须结算。
- `tests/test_round2_terminal_output.py`：PTY EOT/PIPE、root先退同PGID尾部、drain/reader/watcher/timer、post-spawn rollback/8槽/retention/cursor/artifact。
- `tests/test_round2_terminal_environment.py`：原default-deny/probe/single-flight/cache/close/超时与凭据边界。
- `tests/test_pr03_process_termination.py`、`tests/test_terminal_cognitive_subtraction.py`：真实命令、多轮input/取消不重发、foreground publication与自然终止身份。
- `tests/test_round2_terminal_monitor.py`、`tests/test_round2_terminal_safe_point.py`、`tests/test_round2_terminal_architecture.py`：唤醒/取消/完成、provider边界、现有oracle/窄工具面。
- `tests/test_stage2_terminal_host_lifetime.py`：Host关闭/取消/唯一physical owner、失败隔离、writer期限；核实无共享PG fixture需求再执行。
- `tests/test_round9_2_hook_subsystem.py`：真实executor stdin/secret/env/deadline/host close及剩余hook合同。
- `tests/test_mcp_transport_lifecycle.py`、`tests/test_mcp_credentials.py`：真实stdio blocked stdin/TERM忽略/close，SDK连接关闭及合成credential边界。

如首组未覆盖新增MCP task启动故障/重复cancel或hook进程组尾部，可在本目录补有限真实OS probe，使用原owner+自有PIDs/temp files，不复制生产实现或改测试断言。当前nativeMac未configure_interop，create_scope应为None；Windowsscope实测不冒充Mac结果。

## 次组：需要root自建PG

`tests/test_round2_terminal_host.py`与`tests/test_round2_terminal_postgres.py`显式使用stage2迁移fixture，需要root先提供自己创建的私有测试PG和DSN。取得前不运行，不使用默认/保存/系统PG fallback。CI/fixtures不得因缺PG假skip；若前提未就绪则报告待执行。

## 结果要求

原suite实际计数、完整日志、任何failure/skip及物理cleanup分别记录。原Windows131共享/28实机结果只是选测依据，不是此次Mac native计数。发现真实回归先报root涉及生产文件与复现，避免交叉编辑；不先实施生产修复。
