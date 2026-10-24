# Phase A runtime/host 实现记录

2026-10-24。仅实现冻结规格 Phase A；root 已确认本轮改前基线并授权编辑后开始。未进入 B，未改生产 Python、settings 或数据库，未运行 G0/WSL/provider。

## 改前基线与协作

root 提供本轮 before：desktop Node 31 passed、Python 桌面相关 23 passed；startup cover 五场景与资源 relocation/PG/vector/BM25/Chromium smoke 由 root 留存 `root/phase-a-implementation/before/` 的实际记录。本文不把前轮 24/49 测试数算本轮基线。

协作文件范围：本专题拥有 `runtime.ts`、`host-operations.ts`、`host-posix.ts` 和对应测试；desktop 专题拥有 locator/main/CLI，backend 专题拥有 PG owner/physical port。统一首次 npm build/test 由本专题执行，避免多人覆盖 dist；后续 final verification 归 root。

## 实现

- `HostOperations` 只提供当前真实消费的五个方法：同步 `resolveHome(pythonExecutable)`、真实 pipe3 `spawn`、有限 `execFile`（仅 env）、`prepareRuntimeDirectory(home)`、`appendRuntimeLog(home,chunk)`。
- `posixHostOperations` 无状态，直接使用原 Node execFileSync、spawn、promisify(execFile)、mkdir/appendFile。目录与 log 的 POSIX path.join 留在叶实现；mode0700/0600、原 stderr bytes、Node default buffer/原 error/code/stdout/stderr 均保留。
- `PythonRuntime(paths,home,onEvent,host)` 四参必传；已解析 Python/Playwright 路径来自同一 DesktopResourcePaths。只替换物理调用，没有 default ctor/旧签名 fallback、通用进程 wrapper/新状态 owner。
- preparing/preparation、单 child、ready、JSONL校验/start frame、inspect correlation、停止等待、日志失败事件、stdin错误和取消行为保持原样。没有提前实现 B 的 carrier unknown/stopped、TLS、OAuth、WSL helper。
- 原 Node PG port 自己保留 4MiB exec 配置，没有为了去掉一行 SDK调用增加 host工厂依赖。未用的 HostExecOptions.maxBuffer 候选字段及其单独 SDK conformance测试按 root review 删除。

## 验证

运行 `npm --prefix desktop test`，包含 tsc 与全部 desktop Node tests。首次编译成功，45 条中 43 pass/2 fail：两条新 packaged main composition 测试夹具漏原有 `app.setAsDefaultProtocolClient`，由 desktop 专题补 mock，无生产修改/断言放宽。补齐后统一复跑结果 **45 passed / 0 failed / 0 skipped**，duration 7443ms；原始输出在本目录 `npm-test-candidate.log`。

runtime 原 8 条实际 OS child 测试保留，补 runtime 目录失败不得 spawn/prepare PG，以及日志失败仍走原事件且不夺 shutdown 所有权。host 真实 OS 边界覆盖同步 home resolver/ENOENT、literal Unicode/引号/空 argv与显式env/nonzero输出、真实pipe3/cwd/binary bytes、private目录/log与文件错误。

这只证明本机 Node/受测 POSIX接缝。A 所需 Python/资源/已安装 macOS GUI/native Ubuntu 各架构资格仍由 root 汇总；未声称 A 全验收完成或 Windows激活。
