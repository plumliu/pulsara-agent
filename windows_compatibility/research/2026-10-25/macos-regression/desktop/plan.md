# a57ce3b1 Mac原生共享前端回归计划

2026-10-25，desktop专题，gpt-6.1-sol/high。当前Git工作树初始clean；源码候选a57ce3b1。此计划不使用历史Windows结果代替本机Mac结果；不接触/Applications实例、CLI、settings、DB或前台GUI。

## 源码与现有证据复核

- 当前测试入口为frontend/vitest.config.ts，扫描tests/frontend/**/*.test.{ts,tsx}；package只有test/lint，类型用本地tsc --noEmit，并指定个人目录tsBuildInfo避免项目增量缓存写入。
- App全局K/N快捷键使用(metaKey || ctrlKey)，排除Alt/AltGraph，并先忽略isComposing/229。现app-shortcuts.test.tsx已有普通Meta K/N，IME/229负例目前只显式Ctrl；SessionSearchDialog保留自己composition-ref和keydown保护。
- writer-recovery报告描述共享恢复链：普通offline仍可重连，SESSION_RECOVERY_CLOSE_FAILED retryable=false禁止本进程无效重连、技术原错误默认折叠；现PulsaraApp头两项覆盖该行为，原重连/草稿/全局目录选择测试仍在完整suite。
- LocalHttpRuntimeAdapter.pickWorkspaceDirectory只有host.platform===win32才动态加载Linux用户picker；Mac/Linux仍调用原host.chooseDirectory、原取消/abort语义。现runtime-adapter原native-picker测试未显式platform，建议聚焦验证时补明确darwin/真实Mac路径及取消/abort，不以undefined平台替代Mac分支资格。
- main.ts只在win32建立WslSetup/TLS Session/guest PG；Mac同步resolveHome、POSIX PG、HTTP127.0.0.1、userData/CLI仍原路径。Mac目录IPC仍desktop存在时defaultPath desktop，否则home，properties openDirectory+createDirectory；WindowsUNC逻辑在独立分支。
- package.json现在mac/linux各自保留原resources+CLI extraResources；全局beforePack windows-package.cjs先判断electronPlatformName，不是win32立即return，不调用WindowsPython。此为只读核查，实际package/nativeGUI由root统一验证。

## 待统一依赖就绪后执行

1. 使用root统一的native mise Node，不npm ci、不Vite build、不写desktop/dist/build；记录实际Node平台/架构/版本与Git候选。
2. 完整 `npm --prefix frontend test`，保存全部stdout/stderr/退出码；这是完整Vitest/jsdom，不冒充硬件IME。
3. frontend cwd下本地 `tsc --noEmit --tsBuildInfoFile <个人output>/frontend.tsbuildinfo`；`npm --prefix frontend run lint`，分别保留日志。
4. 现有完整suite覆盖App快捷键、搜索/rename/composer IME、恢复UI/草稿/重连、原Mac布局/窗口控件与workspace相关合同。必要时补有实际新分支意义的小测试：明确darwin的原native picker不发Linux目录HTTP；Meta K/N在IME/229/Alt下不触发；Meta K在搜索composition时不关闭、composition结束后可关闭。若需补测试先报root文件/范围，不改生产求绿。
5. 若失败先保原日志与准确文件/断言，区分候选逻辑回归、依赖/运行前提或harness；不删断言、skip/xfail。真正源码回归先向root报文件再窄修，随后相应聚焦复验及必要全套。

## 实证边界

硬件中文输入法候选窗、真实⌘键、原生openDirectory/createDirectory UI、恢复界面实际Electron绘制与安装包来源由root统一CUA；我不抢前台。不能完成的真实硬件IME留待验收；不执行OS sleep/wake。Vitest合成DOM组合事件只证明应用处理器不吞事件，不能证明macOS输入法的真实默认输入。

最终report包含源码定位、命令/完整日志、实际结果/计数与环境、无新增修改或窄修diff、包装/POSIX分支核查、root证据交叉审阅及所有未验收项。
