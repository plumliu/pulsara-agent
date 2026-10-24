# Phase A 最终代码交叉审阅

2026-10-24。只读复核当前全部 Phase A 生产差异、新增模块及测试；本轮未构建/运行测试，root 统一 final npm/mise verify。用户已明确将 Ubuntu 验收记为待验收。

**最终代码审阅结论：无冻结阻断，可收口本次 Phase A 实现；Ubuntu native/GUI/相应安装资格仍待验收，不能报告全部 A 验收完成。B/G0-W/Windows 支持均未进入或激活。**

## PG raw/resolved 修复

已逐行核对 `postgres.ts`、`postgres-operations.ts`、`postgres-posix.ts` 及新增第八条独立 child 回归。

- `readInstance(directoryForm: 'raw' | 'resolved')` 必传，返回实际用到的 `directory/socket`；没有旧 API/default 分支。startup 的 verifyRunningInstance 用 `'resolved'`，reset 用 `'raw'`，不再每次预先计算两个字段。
- POSIX 叶只有 resolved 分支调用 `path.resolve(lines[1] ?? '')`；raw 分支直接取 `lines[1]`，缺失/相对/别名行仍由原 reset 的确切比较拒绝。原 startup 可接受 resolved 等价目录、reset 要原始准确拼写的区别保持。
- root 所指出的真实异常差异已闭合：当 Electron/调用者 cwd 已删除且 postmaster.pid 缺目录行时，reset 不会为一个本来只需 raw 比较的读取去访问 process.cwd；仍得到原中文 mismatch，尚未 stop/remove、startedHere 保留。
- 新测试在独立 Node child 内创建/删除该 child 的 cwd，并先证实 process.cwd 抛 ENOENT；原测试 runner 不 chdir。真实 POSIX port/文件与 fake pg_ctl 被调用，断言原中文 mismatch、原 PG_VERSION 内容和 startedHere，而非放宽为任意 rejection。没有 skip/xfail 或实现镜像来遮蔽该反例。
- 这是真实机械等价修复，不是新增 B 的锁/ready/helper/unknown/WSL 语义，也未扩大 PG 状态 owner。前轮遗漏已在 round1 记录中明确纠正。

## 全部 Phase A 最终范围

当前生产改动限 `desktop/src/{main,runtime,postgres,cli-install}.ts` 与新增 `resource-paths.ts`、`host-operations.ts`、`host-posix.ts`、`postgres-operations.ts`、`postgres-posix.ts`；对应 main/CLI/runtime/PG/host 测试接齐新真实接口。

- 纯 locator 的五字段均有当前 consumer。main 同步 home resolver/错误行为和单一装配保持；同 paths/home/host 流入原 owner。CLI 准确 Python 路径替代 root 参数后脚本 bytes、quoting、package /opt 判定与注册/PATH 业务保持。
- runtime 只把原进程/文件机制委托给 POSIX 叶，保留原 env/cwd/pipe3、默认 finite exec 缓冲/错误、0700/0600与 byte log。preparing/start/ready/inspect/stop、EOF/原取消及 callback/错误均未重写；没有提前修 B 的 stopped/carrier unknown。
- PG owner仍唯一维护 initialized/startedHere与调用顺序，leaf保原 filesystem/argv/LC_ALL/4MiB和实际错误。raw/resolved修复后不再引入额外 cwd 求值。
- 当前没有生产 Python/terminal/file/协议/设置/数据库变化，没有 durable event/registry/fingerprint、任意执行 RPC、总任务寿命cap或 old/new fallback；未增加 WSL/TLS/OAuth/provision 生产路径。
- 本切片的新方法/字段来自实际 Node consumer，不把 future Windows 驱动合同固化为全仓库平台框架；未用 maxBuffer 候选已删除，PG自身实际4MiB需求保留。

## 验证事实边界

此前本人运行过的统一候选 Node run 为编译成功、45/45、0skip，记录在 `npm-test-candidate.log`；它早于最后 raw/resolved 修复，不能当作新第八条 PG 回归已经通过。当前最终 npm/mise verify 与原 native macOS/资源/GUI 结果由 root 负责记录。本轮只读复核不制造新测试通过声明。

Ubuntu native/GUI待验收是用户明确修订的交付状态，不是用 Mac/mock 伪造通过；保留具体待验收清单即可，不阻止对现有本机实现的收口。任何后续 A/B资格/Windows支持声明仍按根规格及用户后续授权执行。

后续已只读核实 root 最终46/46（含最后PG独立child回归）及真实PG/kernel生命周期结果，并按用户并行实验授权独立运行最终dist+随包Python的start前拒绝/EOF/无PG探针；全部通过。具体命令、原始输出与最终证据边界见本目录 `evidence-review-final.md`、`runtime-eof-real.cjs/.log`。该追加不改变Ubuntu待验收或未进入B的状态。
