# Phase A runtime/host 操作切片：改前只读记录

2026-10-24。仅 Phase A；root 正在取得改前基线，当前未编辑生产/运行测试。

## 已读边界

- 根 Windows 规格 §3.2、§11.1、§13.1；A 不实现 WSL/TLS/OAuth/guest helper，不修 B 的 carrier unknown/stopped 语义。
- `desktop/src/runtime.ts` 全文及现有 `runtime.test.ts` 8 个真实 OS child 用例。
- `desktop/src/main.ts` 的同步 home resolver/构造/boot/quit/reset caller、`main.test.ts` 的模块注入方式、desktop package 的构建/测试命令。
- 生产 Python、terminal、PG、settings/数据库不属于本切片编辑范围。

## 拟窄契约（等待 root 最终协调）

资源 locator 由 desktop 专题拥有；候选 `DesktopResourcePaths` 为 readonly `root/pythonExecutable/playwrightBrowsers/postgresBin/packageCli`，runtime 只消费前述已解析 Python/Playwright 值，不再从 root 拼接二进制。

HostOperations 仅实际被使用的方法：

- `resolveHome(pythonExecutable): string`：保持 main 当前 execFileSync 同步 resolver 及 trim；中文错误/退出仍由 main owner 处理。
- `spawn(file,args,{env,cwd}): ChildProcessWithoutNullStreams`：POSIX 固定 pipe3，保留 Node 原 child/events/streams，不增加自制 child wrapper 或完成 policy。
- `execFile(file,args,{env,maxBuffer?}): Promise<{stdout:string;stderr:string}>`：Node promisify(execFile) 原 rejection/真实 code 透传；wait-owner 默认 maxBuffer 不变，PG 若复用则由其原 owner给现有 maxBuffer。
- `prepareRuntimeDirectory(directory): Promise<void>`：当前 recursive mkdir mode0700。
- `appendRuntimeLog(file,chunk): Promise<void>`：当前 append mode0600；原 runtime fire-and-observe error 事件不变。

runtime 显式注入同一个 host 实现与 locator 结果；无旧 ctor alias/default fallback。runtime 仍独占 preparing/preparation/child/exit/reachedReady/request/inspections/JSONL/send/stop 业务。env 构造和启动帧门仍位于原 owner，host 不读 settings、不产生第二状态机。

## 保留的真实回归

现 8 条覆盖单 child/私有 start/inspect correlation；并发 stop 等物理 child；malformed frame EOF；准备中 quit；ready 后 close1；PG准备期间提前 spawn但无 start/inspect；PG失败 EOF并join；child提前退出而PG准备仍结算。它们继续通过实际进程/管道测试 port 注入后的业务，不改为薄函数转发断言。

仅按实际缺口补：host 端真实同步 home resolver、真实 finite exec 的 stdout/stderr/nonzero identity、runtime 原日志 bytes/mode，以及目录/日志操作失败仍从原 owner通道报告。避免照抄接口逐行 mock测试。

## 协作契约收敛

root 改前基线已确认：Node 31 passed、Python desktop 23 passed，startup/资源 smoke 由 root 留原始记录。root 已授权本切片编辑，且协作确定 `PythonRuntime(paths,home,onEvent,host)` 四参必传；`prepareRuntimeDirectory(home)` / `appendRuntimeLog(home,chunk)` 的 POSIX path.join 位于叶实现。

PG 独立 POSIX port 自己保留原固定 binary/4MiB exec；HostExecOptions 的候选 `maxBuffer` 没有生产 consumer，按 root review 删除，并删除其单独 SDK maxBuffer测试，不增加未来占位。当前 finite exec 仅暴露原 wait-owner 实际需要的 env，Node 默认 buffer/错误行为保持。

## 当前状态

我的源码/测试已就绪，等待 backend 就绪后由我首次统一编译/运行 desktop Node测试，避免协作同时写 dist。尚无本轮 candidate test 通过声明；前轮 24/49 条不能作为这轮基线。
