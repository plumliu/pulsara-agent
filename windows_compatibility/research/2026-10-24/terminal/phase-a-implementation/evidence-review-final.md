# Phase A 独立真实资源实验与最终证据审阅

2026-10-24。按用户并行实验授权，在个人 output 编写并运行独立 disposable probe；不改生产/测试文件，不运行 tsc/npm/mise，不接PG、保存settings、用户实例或CLI。仍仅 Phase A，不进入 B/G0-W/provider。

## 独立实验：真实随包 Python 的 start 前失败/EOF

运行目录：仓库根。实际命令：

```sh
node output/windows-compat-20261024/terminal/phase-a-implementation/runtime-eof-real.cjs /Users/plumliu/Desktop/python_workspace/pulsara_agent > output/windows-compat-20261024/terminal/phase-a-implementation/runtime-eof-real.log 2>&1
```

命令退出0，耗时约2.04秒。脚本直接 require 最终 `desktop/dist/{runtime,host-posix,resource-paths}.js`，不重建dist；通过正式 locator 选 `desktop/resources/python/bin/python3`，不使用系统Python、假Python或checkout脚本替代。临时home经realpath得到短 canonical `/private/tmp/prt-eof-tFGotX`，只注入该独立PULSARA_HOME。

preparePostgres callback 等真实子进程 spawn 后抛同一个受控 Error，不启动任何PG，不返回DSN、不发送start帧。实验断言及完整输出见 `runtime-eof-real.cjs` / `runtime-eof-real.log`：

- 原runtime调用正式stdin EOF分支；真实Python PID70597按顺序 exit0、close，signal=null，stdin.writableEnded=true。
- `runtime.start` 拒绝严格保持原Error对象身份，未被ready前退出错误覆盖；回调只调用1次。
- stdout协议事件准确为 stopping/stopped，未发 starting/ready/failed；runtime.running=false，kill(pid,0)返回ESRCH，未留本次kernel。
- EOF结束后的desktop目录为空，没有kernel lock、diagnostics/socket/PG/log等执行残留；home未生成settings或PGdata。
- 接着两次用同一正式runtime方法独立执行随包Python `desktop_runtime --wait-owner`，均正常结束，证明没有锁owner残留。此方法按原设计留下 `kernel.lock` inode，不代表flock仍持有；实验最后清除整个临时home，未将正常锁文件保留误报为泄漏。
- 再调用原runtime.stop可完成，不引入第二child或PG操作。脚本finally只清自己的临时home。

这补足了原Node测试fake子进程与真正随包Python/installed package入口之间的具体失败路径证据，没有重复backend的真实PG启动实验。

## root 最终证据复核

只读检查 `root/phase-a-implementation/verify-final.log`、`real-posix-lifecycle.cjs/.log`、`python-final.log`、`startup-final.log`、`browser-final.log`、`wheel-outside-checkout.log`，结果与其实现相符：

- verify-final包含tsc/Node **46 passed、0 failed、0 skipped**；其中第35条是最后补的“deleted cwd + malformed PID”的真实独立child回归，不能再引用较早45/45替代该证据。
- 同一verify重新构建应用wheel/renderer并保留未变native SDK cache；实际资源smoke显示PG、vector0.8.1、pg_search0.26.1、BM25查询、Python/Chromium relocation通过，release artifact check版本为0.1.0-alpha.6。
- `real-posix-lifecycle.cjs` 使用最终dist的正式PG operations与PythonRuntime，在独立canonical temp home中执行真实PG init/reuse、BM25/vector、显式reset、fresh installed wheel ready/HTTP200/inspect/正常stop/锁释放、重开再stop。日志明确所有断言通过，事件两次starting/ready/stopping/stopped/exited，与脚本准确对应。
- Python桌面相关23 passed；startup cover五场景通过；Electron browser 11 passed/0 failed。browser故意访问unsafe port的预期失败仍保留，不被当作产品失败或删证据。
- outside-checkout记录的Python和pulsara_agent来源为 `desktop/resources/python/.../site-packages`，支持fresh installed wheel断言；不是candidate editable import冒充打包资源。

没有把这些本机darwin/arm64证据当native Ubuntu/WSL/Windows资格。未检查到的Windows网络/pipe/锁/NSIS仍完全属于B/G0-W。

## 最终结论

**Phase A 最终实现与现有本机证据无冻结阻断，可交付本次POSIX宿主接缝实现。** root已闭合PG raw/resolved异常行为，46/46及真实资源/生命周期加本独立EOF失败路径相互补足；没有新owner、durable authority、总寿命cap或B生产内容。

Ubuntu native与相关GUI/安装回归按用户明确决定**待验收**，当前不得报告全平台A验收完成，更不得宣称Windows支持激活。后续进入B仍需另有授权并满足根规格G0-W前置。
