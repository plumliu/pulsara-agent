# 待运行的真实 provider terminal 验收

**当前状态：用户GUI配置后，真实执行与精确清理已经完成。** 具体通过、原失败和范围见[执行报告](report.zh.md)。下文保留准备阶段的接口说明；两个private harness目录均已退休，不能继续使用旧路径。

本目录只是现 dogfood runner 的测试适配，不是新 runtime。`run_terminal_wsl.py` 复用 `tests.dogfood.run_terminal_cognitive_dogfood.run`、原 ModelRuntime/KernelHostCore、原 migration owner，以及已有 `_RecordingModelRuntime`。只在这个测试进程内替换 runner 的 settings/DB/recording/temp factories，退出后恢复。生产文件与 runner 文件未改。

新正式be6af候选GUI ready后，已仅只读核当前PG（`current-installed-pg.json`）：systemid7700523439330484737、PID535、17.9、精确data/socket、无TCP、原deep verifier verified、sessions0。未读取模型settings。已将当前tests与harness拷入私有ext4 `/home/plumwsl/workspaces/.provider-accept-xqgw_1ur`；正式installed Python先导入正式安装包，再只加入该目录寻找tests，清除继承PYTHONPATH，`--check-imports`通过。`installed-harness-preparation.json`给出实际包路径。这里只保留待运行测试材料，没有进程/PG/scope运行。

`prepared-target.json`只含当前核准的非秘密参数；connection-id/model/output等待root在用户配置完成后提供。它不是执行权限或可重放任务，真正execute仍会重新核验当前PG事实；目标改变必须重新观察，不根据本文件跳过检查。

`preparation-result.json` 已证明 Windows Python `--help`、Ruff、WSL Python `--check-imports` 成功。没有调用 execute，没有读取保存 settings、创建 scope、连接数据库或调用 provider。首次 lint 的动态 locals 导出被 Ruff 判 unused；改成显式 namespace 导出后通过，运行语义未变。

执行需 root 给出全部非秘密事实：saved-home、connection-id/model、已装 distribution/user/UID/helper、已核准可丢弃 PG17.9 的 data/socket/port/system identifier/admin-role/runtime-role/root-database、独立 scratch-root、全新 output 路径。命令没有 key/password/token 参数；模型凭据只沿 LocalSettingsStore→只读内存 snapshot→原 ModelRuntime。不打印配置。DSN 由 Unix socket 与角色参数构造，不启 TCP，不改保存 PG 配置。

在选定安装环境的 Linux Python 中，让当前仓库 tests 与候选 pulsara_agent 源码/安装包在 sys.path；不要误用共享 Linux clone 的旧源码。可由 root 现有隔离 source payload 的 Python 启动，显式路径示意：

```text
<selected-guest-python> <this-script> --execute \
  --saved-home <actual-saved-home> --connection-id <saved-id> --model <saved-model> \
  --distribution <selected-distro> --user <selected-user> --expected-uid <uid> \
  --interop-helper <installed-guest-absolute-PE-path> \
  --socket <approved-unix-socket-directory> --port <port> \
  --admin-role <admin-role> --runtime-role <runtime-role> --root-database <existing-db> \
  --expected-data <approved-data-directory> --expected-system-id <SQL-system-id> \
  --scratch-root <owned-native-ext4-directory-outside-saved-home> --output <new-json-path>
```

以上是参数说明，含占位符不能直接执行。`--execute` 缺任一事实会拒绝；无默认目标/模型连接。默认有限 case 集合来自原 runner：directories/interaction/pty/long/truncation/monitor_race/monitor/monitor_long_wake/capacity。中文空格临时路径在核准 ext4 scratch-root 下建立；可通过 `--cases` 选原有子项，不新增产品轮数上限。互操作配置调用唯一 `configure_interop`，不会靠 WSL 标签或裸 WSL fallback 假称覆盖 Job；这些 case 本身主要运行 Linux 命令，**不等于实际 Windows PE 正常/取消已验**，后续跨域场景须按计划补证。

PG 预检要求当前17.9/指定data与socket/systemid、无listen_addresses、无inet地址、准确admin/runtime角色同postmaster。仅创建本次 UUID DB、原 clean-v0 两次migrate并检查首次(0,)/再次空；finally只DROP本次创建的 DB。绝不启停/reset PG集群。目标改变时拒删并在 undropped_databases 中保留，不能把未清理当成功。正常session/core shutdown完后才清scratch；任何异常保留scratch供精确复核，外部收尾不得冒称产品成功。

输出包含完整 provider context projection、响应normalized blocks、prompt/reply、tooltrace、DB事实及原case断言。调用沿原录制适配器，没有复制provider协议。记录用 saved.credential_scrub_set 精确剔除实际全部已配置秘密，并做contains后置检查；不宽泛隐藏错误正文。不自动重试已发生的副作用、不覆盖既有结果。

本 harness 只补原owner+provider的terminal证据。Electron/TLS/GUI、MCP/hook、真实Windows后代、subagent/compaction、文件往返和关闭后历史/memory仍以旁边 `phase-b-pg/provider-acceptance-plan.zh.md` 的各批次为准，不能由本脚本通过替代。等待用户在GUI保存配置后，root统一提供实际目标并执行。
