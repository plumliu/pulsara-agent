# Phase A runtime/PG与桌面装配交叉审阅

2026-10-24。审查实际当前源码，与Git改前路径逐项对照；没有改其他agent负责文件。结论：**当前Phase A代码范围内未发现阻断**。不代表完整A原生安装/native Linux资格已完成。

## Runtime与POSIX host

- HostOperations只含当前main/runtime实际需要的同步home、stream spawn、finite execFile、runtime目录与日志操作；无未消费maxBuffer配置、平台registry或WSL/TLS接口。
- `host-posix.ts`完整保留原execFileSync Python resolver、trim与失败抛出；spawn三个pipe、cwd/env及execFile真实退出错误。原700目录/600append日志挪到同一叶子，仍用同一个home。
- runtime真实caller全部消费单次解析pythonExecutable/playwrightBrowsers；prepare/start frame、ready/failed、inspection2秒UI查询、EOF/child close和stop逻辑未做产品变化。main仍同步resolveHome后构造原single owner，原boot/reset/quit内容不变。
- 控制通道故障后child清空等WSL-only未来修复未带入A，原SIG/PTY/文件安全Python机制完全未改；本次不宣称支持Windows。

## PG物理port

- `postgres.ts`只有唯一initialized/startedHere与生命周期、DSN、原104字节socket协议边界；不再有Node fs/exec或本机binary布局拼接。
- leaf执行原目录prepare、PG_VERSION读取、postmaster.pid读取、socketmode、realpath/uid、固定initdb/pg_ctl执行与rm；无自主重试/start/stop/reset/DDL或settings副本。
- 特别核对start接受resolvedDirectory、reset要求rawDirectory精确拼写这两个原不同谓词，未被一套metadata equality覆盖。reset parentrealpath短路、ENOENT/其他异常、numeric status3、readiness失败保原错误与verifiedstartedHere、reset stop失败/删除失败不清owner均保留。
- mode/UID与nofollow等当前原生POSIX机制未被加强/削弱；原已存在的文件检查边界不借A夹带typedbinding/新G0/helper。

## 测试与剩余资格

terminal为唯一首次协作构建执行者，实际`npm --prefix desktop test`最终 **45/45通过，tsc成功，0skip**；日志`output/windows-compat-20261024/terminal/phase-a-implementation/npm-test-candidate.log`。最初43/45仅因新增packaged main fixture漏既有`setAsDefaultProtocolClient` mock，已补测试夹具后统一重跑；未改生产/弱化断言。

已读新host真实OS tests（resolver同步失败、argv/env/非零码、三个bytepipes/cwd、私有日志及失败）和PG7条（真实fixture/链接拒删+原owner失败/实例谓词/argv/DSN/长度边界），覆盖实际新接缝，没有复制SDK完整conformance或为每个转发方法造镜像测试。

root继续做Mac资源/候选包与安装验证；我不并行写dist，不碰用户正在运行的`/Applications/Pulsara.app`。Ubuntu native x64/arm64和Ubuntu GUI环境缺项必须按冻结spec如实记录，Docker Linux容器通过只能作userspace补充。

## PG真实边界修订复核

root进一步发现初版leaf无条件计算resolvedDirectory，会在reset原本只比较raw line时新增cwd求值；调用者cwd已被删除且PID文档malformed时，可能把原中文mismatch变成额外ENOENT。我此前只比对owner谓词，未识别leaf提前观察的这处差异。

已复核backend修为必传`readInstance('raw' | 'resolved')`：startup仅resolved分支调用path.resolve；reset只取原raw字段。新增独立Node child真实删除其cwd，构造status0/malformedPID并断言原mismatch、data与startedHere保留，主runner不切cwd。该修法恢复原真实故障合同，无新增范围，当前未发现剩余阻断。先前45/45为修订前结果；修订后最终统一测试由root/terminal另报，不沿用数字冒充通过。
