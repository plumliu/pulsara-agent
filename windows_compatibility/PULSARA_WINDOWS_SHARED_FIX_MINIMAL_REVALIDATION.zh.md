# 本轮共享修复：Windows最小重验清单

2026-10-25。供Windows端接手对话执行，针对a57ce3b1之后本轮Mac回归产生的三个生产文件修改。本文是**本次差分的最小回归建议**，不缩减主spec的完整发布/激活矩阵，不要求重做已经通过的整套G0。

## 1. 材料在哪里，重验什么

目录的实际名字是 **`windows_compatibility`**。所有下面这些报告都在该目录内，不依赖被ignore的output：

- [Mac汇总](research/2026-10-25/macos-regression/root/report.zh.md)和[四方冻结](research/2026-10-25/macos-regression/root/freeze-review.zh.md)。
- [backend最终审阅](research/2026-10-25/macos-regression/backend/final-root-review.md)。
- [terminal最终审阅](research/2026-10-25/macos-regression/terminal/final-root-review.md)。
- [desktop最终审阅](research/2026-10-25/macos-regression/desktop/final-root-review.md)。

冻结表中的`../backend/...`是相对于`macos-regression/root/`，因此仍在同一个`macos-regression/`归档内。生产代码和正式测试仍分别在仓库原`src/`、`tests/`、`desktop/`，不从归档目录import生产实现。

本轮只有以下产品差分需要新Windows证据：

| 生产文件 | 改动 | Windows重验重点 |
| --- | --- | --- |
| `src/pulsara_agent/conversation_kernel/provider_dispatch.py` | accept之后仅全局head前进时，退休未安装的prospective资源并回同一已accepted turn普通prepare | 单次accept、完整输入漂移仍拒绝、资源一次清理、provider前缀连续及真实context场景 |
| `src/pulsara_agent/hooks/executor.py` | POSIX组存在性EPERM保可能存活；signal EPERM不覆盖原结算 | WSL内POSIX自然尾进程；配置interop时仍走原scope、Windows尾进程退出后才返回/退槽 |
| `src/pulsara_agent/conversation_kernel/mcp/sdk_facade.py` | 同样处理组EPERM；stderr reader原失败仍能交给原session | 普通stdio close与重复取消；跨域MCP调用取消不关闭session，最终close真正join |

没有新增数据库结构、Windows Job协议或前端产品实现。其余变化是测试和文档。开始前必须看**实际Git diff**；Windows若又增加了别的生产改动，要按新增影响扩大范围。

## 2. 最小必做与可暂不重做

| 必做 | 执行位置 | 通过含义 |
| --- | --- | --- |
| R0 同源码、新payload/新安装候选及来源核验 | Windows clone + WSL ext4 clone | 测到本轮修复，不是旧alpha.9 |
| R1 ROOT确定性PG反例、严格负例与原prefix测试 | WSL `.venv` | 新窗口确实触发；不重复accept、不改已安装前缀 |
| R2 hooks/MCP/POSIX定向回归 | WSL `.venv` | 原普通POSIX路径及错误/取消边界保留 |
| R3 10个选定真实Windows PE/WSL集成case | WSL `.venv` + Windows新helper/fixture | 两个域确实join；不只看wsl.exe或Linux PGID退出 |
| R4 Windows shell定向Node及startup smoke | Windows Node/Electron | 宿主装配正常，修改后的测试fixture也能在Windows运行 |
| R5 最终安装包真实provider context + 简短GUI/Quit/重开 | Windows Electron + 已安装WSL Python | 实际制品端到端可用，原会话/模型/工作区保留 |

**R1+R2当前共收集98项**，R3当前10项；数量只是防漏参考，不通过skip/xfail凑绿。本文只在Mac核过路径、源码和test collection，尚未在Windows执行，不能把这份清单本身记作通过。

暂不必重复：全量1372项前端、整个后端244项、全部G0故障排列、Notion重新OAuth、卸载/重装全矩阵、真实睡眠、混合DPI和硬件IME。这里没有修改对应产品owner；它们既有未覆盖项仍保留。capacity/MCP/compaction真实provider、writer到期恢复、完整170项相关Python是扩展核验，可在出现相关失败或另有生产变更时加入。context随机调度成功不保证触发新race，必须保留R1确定性反例。

## 3. R0：同步、版本与构建

1. Mac侧本轮代码、新增测试和归档尚未提交时，Windows不能只pull旧HEAD就开测。先同步包含本轮修改的实际提交，Windows/WSL两clone分别记录`git rev-parse HEAD`、`git status --short`；最终对应同一源码。不要reset别人dirty工作。
2. 沿用原已有WSL发行版/默认非root用户、interop、automount与TUN。以下真实集成fixture当前固定`Ubuntu`/`plumwsl`；执行前核对它们仍是实际目标。不同目标须先明确适配测试参数，不能静默测错用户或把测试改成mock。
3. **正常更新到已装候选时使用一个尚未占用的新本地版本**，由Windows接手agent按既有授权递增，两个clone版本同步。不要将修改后的payload覆盖已部署的同版本alpha.9目录；setup会严格检查同版本payload身份。保持desktop/frontend package与lock、Python版本与uv.lock一致，运行原`release_artifacts.py check`。不打tag、不发布。
4. Linux资源在WSL ext4 clone构建；Windows只构建/携带Windows壳和helper，不能复用Mac的`desktop/resources/python`。

WSL Bash，仓库根目录，所有共享构建串行执行：

```bash
mise run --jobs 1 desktop:verify
mise run --jobs 1 desktop:wsl-payload
```

将**本次生成的两份payload制品**拷入Windows clone；这是原打包链的制品传递，不共用venv/node_modules：

```bash
# 修改为已核实的Windows clone在WSL中的绝对路径。
WIN_REPO=/mnt/c/python_workspace/pulsara-agent
mkdir -p "$WIN_REPO/desktop/resources/wsl-payload"
cp desktop/build/wsl-payload/payload.tar "$WIN_REPO/desktop/resources/wsl-payload/"
cp desktop/build/wsl-payload/manifest.json "$WIN_REPO/desktop/resources/wsl-payload/"
```

Windows **cmd.exe**，Windows clone根目录；现有已准备好的mise/uv环境继续使用，不需要PowerShell：

```bat
mise run --jobs 1 desktop:windows-resources
mise exec node@22.23.3 -- npm.cmd --prefix desktop run build
```

每条检查退出码。`desktop:windows-resources`会验证payload版本/完整摘要并编译新helper。Node、Linux构建、Python测试不要同时改写同一clone的资源/依赖。

## 4. R1/R2：WSL源码定向测试

工作区必须是WSL ext4 clone；使用`.venv/bin/python`，**不在Windows原生Python里运行POSIX pytest**。R1需要PG17及当前扩展、admin/runtime两个角色；优先复用已经验证的独立测试cluster。只通过下面两个测试专用变量指向它，不能未经核验指向日用数据库：

```bash
export PULSARA_POSTGRES_ADMIN_DSN="host='/实际私有socket' port=5432 dbname=postgres user=pulsara_admin"
export PULSARA_POSTGRES_DSN="host='/实际私有socket' port=5432 dbname=postgres user=pulsara_runtime"
```

这两个是pytest fixture已有入口，不是production credential fallback。fixture创建/迁移/drop独立UUID数据库，`CI=true`让缺库直接失败而不是skip。不要source `.env`、导出模型key或修改saved settings。

没有现成独立cluster时，可在**刚刚构建资源的WSL clone**用下列步骤建立本次私有cluster。变量保持在同一Bash会话，路径不是文档中的历史Mac socket：

```bash
PG_BIN="$PWD/desktop/resources/postgres/bin"
PG_TEST_ROOT=$(mktemp -d /tmp/pulsara-win-recheck.XXXXXX)
chmod 700 "$PG_TEST_ROOT"
mkdir -m 700 "$PG_TEST_ROOT/s"
"$PG_BIN/initdb" -D "$PG_TEST_ROOT/data" -U pulsara_admin --auth-local=trust --auth-host=reject --encoding=UTF8 --locale=C
"$PG_BIN/pg_ctl" start -w -D "$PG_TEST_ROOT/data" -l "$PG_TEST_ROOT/postgres.log" -o "-c listen_addresses='' -k $PG_TEST_ROOT/s -c unix_socket_permissions=0700 -c shared_preload_libraries=pg_search"
"$PG_BIN/psql" -X -v ON_ERROR_STOP=1 -h "$PG_TEST_ROOT/s" -U pulsara_admin -d postgres -c 'CREATE ROLE pulsara_runtime LOGIN'
export PULSARA_POSTGRES_ADMIN_DSN="host='$PG_TEST_ROOT/s' port=5432 dbname=postgres user=pulsara_admin"
export PULSARA_POSTGRES_DSN="host='$PG_TEST_ROOT/s' port=5432 dbname=postgres user=pulsara_runtime"
"$PG_BIN/psql" -XAt -d "$PULSARA_POSTGRES_ADMIN_DSN" -c "SELECT current_setting('data_directory'), current_setting('listen_addresses'), system_identifier FROM pg_control_system()"
```

逐条确认exit0，并记录实际data_directory/system identifier；没有核对身份就不继续。该fixture cluster仅Unix socket、0700目录，自己的两个角色无密码，不改用户PG或网络设置。`/tmp`在WSL可能是tmpfs，本项只证明provider/进程回归，不作为PG ext4持久性资格；真实安装包数据仍须是ext4。

R1：

```bash
CI=true .venv/bin/python -m pytest -q \
  tests/test_root_prospective_head_reprepare.py \
  tests/test_round3_1_provider_input_prefix_continuity.py \
  tests/test_round5_long_horizon_postgres.py::test_root_child_admission_after_root_accept_reprepares_only_the_accepted_turn
```

当前27项。必须包含：head-only正例；相同/倒退head、binding/payload/permission/context/replay/closure/late outcome漂移拒绝；四种退役失败原样传播且各清理一次；真实PG在accept后插入child，断言ROOT accept仅一次、同turn、SYSTEM/tools不变和messages追加。

R2：

```bash
CI=true .venv/bin/python -m pytest -q \
  tests/test_posix_process_group_settlement.py \
  tests/test_round9_2_hook_subsystem.py \
  tests/test_mcp_transport_lifecycle.py \
  tests/test_windows_interop_failures.py \
  tests/test_windows_interop_relay.py
```

当前71项。Linux未必像Darwin那样产生zombie组EPERM，测试已按平台只在Darwin要求该实际错误；WSL仍必须执行全部三项POSIX物理结算测试，不能新加skip。这里也包含interop失败合同，但仍不能替代下一节真实PE。

## 5. R3：真实Windows后代的最小矩阵

Windows cmd.exe，当前Windows clone根目录，编译**现有测试fixture**：

```bat
if not exist output\windows-shared-recheck mkdir output\windows-shared-recheck
"%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe" /nologo /target:exe /platform:x64 /out:output\windows-shared-recheck\interop-child.exe tests\fixtures\windows_interop_child.cs
```

WSL Bash，ext4 clone根目录，仍核实`WIN_REPO`变量：

```bash
export PULSARA_TEST_INTEROP_HELPER="$WIN_REPO/desktop/resources/windows-interop/pulsara-interop.exe"
export PULSARA_TEST_INTEROP_CHILD="$WIN_REPO/output/windows-shared-recheck/interop-child.exe"
CI=true .venv/bin/python -m pytest -vv -s \
  tests/windows_interop_integration.py::test_normal_no_pipe_descendant_retains_live_slot \
  tests/windows_interop_integration.py::test_cancel_after_linux_root_exits \
  tests/windows_interop_integration.py::test_input_and_eof \
  tests/windows_interop_integration.py::test_same_registry_ab_isolation \
  tests/windows_interop_integration.py::test_hook_normal_and_timeout \
  tests/windows_interop_integration.py::test_mcp_sdk_call_cancel_preserves_session_then_close \
  tests/windows_interop_integration.py::test_real_mcp_supervisor_closes_its_stdio_scope
```

当前10项，前3个各有PIPE/PTY两个参数。保留fixture打印的scope事实：physicalJoin、helper/diagnostic退出、relay tasks/writers、worker线程与目录退休。hook正常路径等待自然尾部，不以强杀伪装成功；取消/timeout才走取消。MCP单次tool取消后仍能echo，最终session/supervisor close才收scope。A/B取消隔离仍成立。只有carrier退出或Linux组空不算通过。

## 6. R4：Windows宿主与新测试fixture

Windows cmd.exe，当前clone根目录，采用现有Windows workflow的定向Node列表，避开专供POSIX宿主的测试：

```bat
mise exec node@22.23.3 -- node --test desktop/dist/backend-paths.test.js desktop/dist/backend-session.test.js desktop/dist/host-wsl.test.js desktop/dist/wsl-setup.test.js desktop/dist/postgres-wsl.test.js desktop/dist/runtime.test.js desktop/dist/browser-policy.test.js desktop/dist/oauth-return.test.js desktop/dist/ui-preferences.test.js
mise exec node@22.23.3 -- node --test --test-name-pattern "^Windows " desktop/dist/main.test.js
mise exec node@22.23.3 -- npm.cmd --prefix desktop run test:startup
```

前两条须检查所选测试确实执行通过；name-pattern造成的非目标用例未执行不能宣传成全desktop suite通过。startup在真实Windows图形会话运行，无需用户持续鼠标操作；必须同时通过真实visible fade与hidden直接结算/IPC放行，不靠延长原2秒、删除断言或改生产逻辑取得通过。

## 7. R5：最终安装包与一轮真实provider

在R1–R4通过后，Windows clone按原配方构建新本地候选：

```bat
mise run --jobs 1 desktop:windows-package
```

正常Quit旧应用后，沿原NSIS更新。不reset用户库；记录更新前后的模型存在性、选定会话/工作区与PG身份。核验新版本exe与WSL部署目录，并读取实际随包`pulsara_agent.__file__`，确认本轮三条修复源文件来自新部署且完整bytes与对应源码相同；不加每文件SHA登记。版本号或健康检查200单独不够。

**真实provider只把context/last_n/mailbox列为最小必做。** 复用[原Windows长程验收](research/2026-10-25/terminal/phase-b/provider-acceptance/report.zh.md)的原scenario与canonical accept gate，或基于[Mac最终context探针](research/2026-10-25/macos-regression/backend/run_provider.py)在本次output写小型Windows适配harness：

- 用新安装的WSL Python `-I`，先导入并断言site-packages路径，再仅添加tests/helpers路径；不能用src/PYTHONPATH覆盖产品。
- saved配置经`LocalSettingsStore(require_pulsara_home()/local-settings.yaml)`只读加载，然后以内存override注入已核验独立测试PG/UUID DB。选Windows实际保存连接，不照抄Mac connection ID；缺配置明确报缺，不造key或改写用户配置。
- 保留两个steer PENDING、唯一mailbox、worker精确ROOT/MIDFLIGHT markers及正常完成、原SYSTEM/tools/messages连续性断言。调用次数按实测记录，不要求恰好等于Mac的8次。
- 必须保留原完整scenario断言。随机provider没遇到新窗口应如实注明；R1真实PG确定性测试才是该窗口的必要证明。
- 最终core/session/terminal/MCP自然收尾，测试DB与home精确清理，原日用数据保持。

**这里不提供一个会误连库的“复制即跑”provider命令。** 归档Mac脚本固定旧repo推导、Mac连接ID与已删除私有PG信息；旧`run_round10_subagent_dogfood.py` CLI入口又要求saved loopback TCP `pulsara`，不能直接用于当前WSL Unix-socket设置。接手agent先读原harness，按上述现有注入点只适配测试入口/路径，不改变产品或scenario；准备后才调用真实provider。不必为它开放PG TCP、更换保存DSN或给产品安装pytest。

最后做一小轮真实Windows GUI：

1. 新包ready；打开更新前原会话，模型与工作区仍准确。
2. 图形新建或选择一个中文/空格目录，创建独立测试会话。可在上一步原context harness之外发送一条简单模型消息；若context已证明provider可用，GUI以一次简单终端命令往返验证即可，不必再跑长程。
3. 留中文未发送草稿，隐藏窗口再恢复，草稿仍在；普通Ctrl+K/N可用。
4. 原确认流程正常Quit：应用/kernel退出，**本attempt实际拥有并应停止的PG**按原owner规则停止；不得凭“所有PG都必须消失”杀掉外部实例。
5. 冷重开后会话、工作区、模型保存配置仍在；未发送草稿按现有内存合同清空。最后正常Quit并清理本次测试连接/进程/文件。

不要求硬件睡眠、重新Notion授权、额外卸载和重新配置用户环境。

## 8. 收尾与可提交材料

若第4节**由本次会话新建**私有cluster：先核验`PG_TEST_ROOT/data`与前面记录的system identifier、无本次测试进程/UUID DB残留，再对这个精确data执行`pg_ctl stop -w -m fast`，保存log，最后删除自己的临时目录。复用外部既有cluster时只清理自己UUID DB，不能套用stop。终端/GUI/模型probe仍活时不先删home或强停PG制造通过。

在`output/windows-shared-recheck/<分工>/`保存新命令/退出码、实际commit/dirty状态、运行时来源、raw失败/成功、scope/DB及清理事实。读取配置只为使用原生产owner；报告只精确移除实际credentials，保留提示/响应/错误，不提交raw settings。成熟报告再放本目录`research/<实际日期>/windows-shared-recheck/`，增加README入口；不要`git add -f output`。

R0–R5全部实际通过后，结论写成“**本轮共享修复的Windows最小回归通过**”，并列仍待验收项。不写“所有Windows场景通过”或“B5激活”；任一失败保留原证据，先定位源码、fixture或环境原因，再决定扩大哪一组，不削弱断言。
