# Phase A desktop实现记录（进行中）

2026-10-24。root确认改前Node31/Python23通过并批准统一契约后，编辑以下限定范围：

- `desktop/src/resource-paths.ts`：纯五字段readonly locator，无I/O、平台配置、manifest或owner。
- `main.ts`：一次解析paths，POSIX host同步解析home；显式注入原Runtime与PG owner。boot/reset/requestQuit/IPC/window/用户home/CLI注册时序原样。
- `cli-install.ts`：launcher只接已解析pythonExecutable；shebang/marker/shellquote/exec/argv/结尾换行未变，registration/PATH/冲突算法未改。
- 对应main/CLI测试：fixture改host/PG operation注入；真实CLI argv/Unicode/quotes/exit/移动/冲突测试接locator。main增加Mac含空格单引号移位bundle与Linux /opt package ownership装配断言，不执行实际应用/PG或用户目录写。

所有生产caller和fixture已同片改签名，无旧签名fallback；main通过同一个paths.pythonExecutable传resolveHome/runtime/CLI。未修改Python、B/WSL/TLS/OAuth/Win32/frontend部署、发布或保存配置。

## 跨专题只读审查

已通读terminal的HostOperations/host-posix/runtime：同步execFileSync resolver位置不变，pipe3、cwd/env、wait-owner、recursive700和append600均沿现Node机制；JSONL、preparation、ready/inspect/EOF/stop状态未改。

已通读backend的PostgresOperations/postgres-posix/owner：readVersion原trim/ENOENT、running-instance resolvedDirectory与reset rawDirectory的不同谓词保留；socket长度/mode、pg_ctl status3与失败后已启动验证、角色DSNquote、reset真实path/uid/版本/状态/删除顺序均保留。POSIX文件/exec只在leaf，owner保唯一initialized/startedHere，无第二生命周期、hash或B helper。

当前只读review未发现阻断。没有把此前spec中的未来WSL fault修复提前带入A。单个leaf没有当前consumer的可选能力应删除；terminal已按root要求移除HostExecOptions中未用maxBuffer。

## 验证归属

root独立进行before baseline与Mac资源/native安装验证；terminal是统一首次tsc/npm test执行者，避免同时写dist。我尚未自行build。测试输出/总数由实际执行者报告后在此补充；当前不能以代码review宣称测试通过。Ubuntu native/GUI缺项见preflight.md。

统一Node验证结果：terminal执行最终tsc与npm test，45/45通过、0skip（日志见其`phase-a-implementation/npm-test-candidate.log`）。第一轮43/45的两处失败仅为新增packaged main fixture缺既有setAsDefaultProtocolClient mock，补齐测试夹具后重跑，无生产/断言让步。详细交叉审阅见本目录cross-review.md；保持源码/dist稳定交root继续原生资源/包验收。
