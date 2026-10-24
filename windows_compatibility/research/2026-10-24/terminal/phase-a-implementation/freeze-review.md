# Phase A 实现与 Mac 证据冻结终审

2026-10-24；terminal / gpt-6.1-sol high。

**同意冻结本次 Phase A 实现与已完成的 Mac 本机/应用包 GUI 证据，无剩余代码或证据阻断。Ubuntu native x64/arm64、Ubuntu GUI/安装资格按用户决定待验收，不宣称 A 全平台已验收；B、G0-W、Windows支持均不进入。**

## 审阅范围

已通读根规格文首、§3.2和§16全部内容，并核对 §15“路线冻结时事实”与后续A实施记录的时态区分。读了root `final-review.md`、`macos-gui-review.md`、`gui-cleanup.log`，backend `final-review.md`、desktop `final-evidence-review.md`；实际交叉读取backend `real-pg-failures.json`、desktop `package-cli-evidence.json`、root package/GUI环境/PID/PG日志与恢复脚本。此前已读最终verify46/46、Python23、startup/browser/真实lifecycle及wheel来源，本人独立EOF probe也已通过。

## 最终结论依据

- 实现仍仅真实Node资源/host/PG physical接缝，原main/PythonRuntime/BundledPostgres唯一业务/状态owner未迁移；所有真实caller同次hard cut，无未来空adapter/第二状态机/fallback。原PG raw/resolved问题及Mac canonical临时根回归已闭合；没有生产Python/terminal/file/协议/provider/schema或B内容。
- root §16把源码实现、Mac资格、Ubuntu待验收、B未进入分开，46/46及23通过与原始log相符。没有用早期45/45、mock或容器顶替最终实测；新TS模块由原tsconfig/dist打包规则覆盖，未虚构native cache输入变化。
- Mac新包来源有18个完整Buffer相等比较，含最终PG raw/resolved与runtime四参模块；两包六条真实私有Python/CLI搬移命令均成功、无注册动作。backend真实PG错socket/raw PID/版本失败后，原实例仍可查询或明确stopped，sentinel/control保留，实际清理状态3；与root汇总一致。
- Mac GUI正常退出/重开、精确测试kernel暂停后的慢退、PG版本99故障及恢复17再ready有明确实际进程/路径和PG正常shutdown日志，慢退未提前停PG、未强杀。测试包仅隔离元数据改动并重签，ASAR业务来源未变化；不能推广为DMG签名/Gatekeeper/Ubuntu/Windows资格，root文档也没有这样声称。
- CLI隔离失误如实记录。HOME没隔离macOS os.homedir导致原best-effort注册短暂更新真实launcher，不被写成全程零副作用；没有改前准确快照，所以不声称恢复未知原始字节。当前正确生产入口恢复与临时保护撤回可核实，后续harness保护要求也已记录。

## 独立只读清理复核

本轮未构建、未运行应用/CLI、未改生产或根规格。为核对最终cleanup事实，仅执行文件观察与指定PID的ps：

- `/Users/plumliu/.local/bin/pulsara`：`lstat.isFile=true`、`isSymbolicLink=false`、mode具备执行位；全内容准确等于当前 `cliLauncherScript('/Applications/Pulsara.app/Contents/Resources/python/bin/python3')` 输出。未执行该launcher，也未修改它。
- `/private/tmp/paGUI-78k3eg1_` 已不存在，和root `gui-cleanup.log`一致；本实验 `/private/tmp/prt-eof-tFGotX` 此前已由自己的finally删除。
- `ps -p 20252,20270 -o pid=,ppid=,comm=` 实际输出仍为：

```text
20252     1 /Applications/Pulsara.app/Contents/MacOS/pulsara
20270 20252 /Applications/Pulsara.app/Contents/Resources/python/bin/python3
```

原应用与kernel仍活着；不将测试实例清理误称原用户实例退出。root原test PID与PG日志/--wait-owner清理结果相符。当前 `git diff --check` 亦无输出，root ruff log为All checks passed。

## 冻结边界

根规格第16节、root最终报告与三专题证据在支持范围、实际实现、已通过Mac结果和待验收事项上无矛盾。可仅更新最终批准/冻结元数据后交付；无需再为本轮收口扩接口、追加机制或重复构建。Ubuntu资格后续单独补证；进入B仍需用户授权及原G0-W前置，当前不宣称Windows支持。
