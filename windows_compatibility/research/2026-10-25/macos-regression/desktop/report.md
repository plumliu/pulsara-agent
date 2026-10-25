# a57ce3b1 Mac共享前端回归

2026-10-25。gpt-6.1-sol/high。结论：**本专题完整前端回归通过，未发现需要生产修复的Mac回归。** native GUI、应用包装与真实原生快捷键/输入法仍按root统一证据分别审阅，不能由本次jsdom结果替代。

## 实际环境与结果

候选为 `a57ce3b103214d3a0865e0460c0f7dd0d0aa97a4` 加root批准的两个前端测试文件增量；初始工作树clean。native macOS26.2/25C56，Darwin25.2.0 arm64；mise Node22.23.3，实际execPath为 `/Users/plumliu/.local/share/mise/installs/node/22.23.3/bin/node`。根任务统一完成依赖/前端资源准备后开始，未由我执行npm ci、Vite build、desktop编译或其他共享构建。

| 检查 | 实际结果 | 原始材料 |
| --- | --- | --- |
| 完整Vitest，`--maxWorkers=2` | 83/83文件、1372/1372测试通过，exit0；0skip/0todo；69.99s | `vitest-full.log` |
| frontend本地tsc `--noEmit` | exit0，无错误；增量信息写个人output | `types.log` |
| 完整frontend `npm run lint` | exit0，无错误/警告 | `lint.log` |
| 两测试文件diff whitespace检查 | exit0 | `results.json`命令 |

完整命令/cwd/退出码见 `results.json`。Vitest物理worker并发2只为与其他原生定时/进程实验协调，不新增产品cap。frontend ESLint配置扫描frontend目录；不宣称这条lint对外部tests/frontend目录作了检查。两个新增测试文件实际由完整Vitest执行，tsc按现frontend tsconfig作生产类型检查。未新增skip/xfail或弱化既有断言；本轮首次完整运行直接通过，无先失败后改断言。

## 有意义的覆盖增量（只有测试变化）

root最初明确批准两个前端文件，共46增/11减，增加10个case：

- `tests/frontend/app-shortcuts.test.tsx:18`：原Ctrl Alt/isComposing/229负例完整保留，另补Meta K/N的六项。断言默认行为未取消、两个快捷对话框均未打开，防止组合输入被全局快捷键吞掉。原普通Meta K/N正例仍在，不是只测试禁止输入。
- 同文件39/65行：原搜索组合期、Ctrl+Alt抑制/普通Ctrl关闭测试参数化为Ctrl与Meta，增加两个case。composition-ref期间不关闭，结束后普通Meta K真实调用onClose且preventDefault；ExtraAlt同样不关闭。
- `tests/frontend/runtime-adapter.test.ts:93`：原native-picker测试明确 `platform:'darwin'`，使用中文/空格/单引号及尾空格的Mac绝对路径，保留原null取消断言并要求没有Linux目录HTTP请求。验证Windows新增picker分支未改Mac路径域和原生选择owner。
- 同文件107/120行：两个取消合同。已取消owner不能打开native picker；等待中取消后即使原生对话框晚给出路径，也必须返回AbortError且不发LinuxHTTP。后者证明应用丢弃晚结果，不宣称AbortSignal能强制关闭macOS原生对话框。

## 当前共享行为与Mac/Windows分支核查

1. `frontend/app/pulsara-app.tsx:1744`仍用逻辑key、Meta/Control K/N，排除Alt/AltGraph，组合期/229先返回。`session-search-dialog.tsx:172`保留自己的composition-ref与Esc/Enter行为。完整suite还执行原composer、search、rename的IME合同。
2. `tests/frontend/pulsara-app.test.tsx:18/40`在本轮完整suite执行：writer失效沿原重连链、不提交原输入；永久SESSION_RECOVERY_CLOSE_FAILED说明恢复未完成、无无效本进程重连按钮、原错误在默认折叠details、无新会话提交。原草稿/重连/用户级目录选择等全部原测试同样执行；没有增加第二重连loop。
3. `frontend/lib/runtime-adapter.ts:2231`仅win32动态加载Linux用户级picker，Mac仍await原chooseDirectory并检查晚取消。`desktop/src/main.ts:373`原Mac目录IPC仍使用Electron desktop（实际存在）或home，允许openDirectory/createDirectory；UNC映射仅Win分支。
4. `desktop/src/main.ts:23/31/35/74`Mac仍同步resolveHome、POSIX host/PG、原userData、原HTTP127.0.0.1；WslSetup和HTTPS BackendSession由win32隔开。Mac没有wsl.exe/provision/WindowsPATH、Windows维护门或反向OAuth调用。
5. `desktop/package.json`mac/linux各自保留原resources与bin/pulsara的extraResources，Mac icon/minimumSystemVersion/DMG arm64配置仍在。全局beforePack指向 `scripts/windows-package.cjs`，其 `electronPlatformName !== 'win32'`立即return，不在Mac调用`.venv/Scripts/python.exe`。这是源码只读复核；实际新Mac包来源和运行由root证据证明。
6. 原Mactitlebar/traffic-light与非Mac原生控件分支、会话栏⌘提示仍显式darwin；完整desktop-window-controls/desktop-workspace相关测试未替换成Windows场景。

## 未冒称的范围

本次Vitest在真实Mac上运行，但jsdom合成Meta/composition事件不是物理⌘键或系统中文候选窗输入。真实硬件IME、原生dialog交互、Electron绘制、安装包、用户真实应用状态由root统一GUI核验；无法完成项应保留待验收。此分支未抢前台、未使用CUA、未OS sleep/wake、未读写saved settings、用户DB/CLI或/Applications app。测试取消只属于fixture，没有结束用户进程或发行版。

root统一构建造成的已跟踪static资源输出变化不属于本分支编辑；我没有恢复、暂存或提交别人的构建结果。最终跨专题冻结需结合root、backend、terminal本轮报告与实际GUI证据。

## 追加：native startup测试前提修正

root首跑startup遇到fade never started，原日志保留；我接管后已用同任务renderer观测证明hidden合法快路径已完成destroy与原IPC放行，测试却仍无条件等待fade。经root批准，只补 `desktop/scripts/startup-cover-smoke.cjs` 的真实可见性前提和独立hidden快路径实测，原2秒及全部原断言不变；没有修改生产、加sleep或放宽超时。现dist直接Electron完整三次连续exit0，详见 `startup-analysis.md` 与 `startup-fixed*.log`。修后首个harness自身误返回函数导致structured-clone错误亦完整保留，不冒称该次通过。

本专题最终只改三个测试文件，共95增/14减；Vitest/type/lint仍对应上述完整前端运行，后续仅改Electron smoke脚本不影响此前前端结果。startup foreground fixture已按root授权序列完成、退出并归还前台。
