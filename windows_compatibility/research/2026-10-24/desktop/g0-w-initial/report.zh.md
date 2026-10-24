# Windows 宿主准备与 G0-W-N/F 子项实测

日期：2026-10-24。仓库：`C:\python_workspace\pulsara-agent`；测试时 Git `da397a2bb701c6371ed19155f66328334684bf38`。仅准备宿主依赖和运行隔离实验；未改生产代码、package/lock、保存配置，未运行生产 main、`npm start`、package 或 Phase B。

## Windows 开发依赖

| 项目 | 实际结果 | 证据 |
| --- | --- | --- |
| mise | 2026.10.12 windows-x64，符合仓库 min_version 2026.7.18；由 root 已启动的 WinGet 安装完成 | `mise-version.log` |
| Node | v22.23.3 win32/x64；`C:\Users\plumlocal\AppData\Local\mise\installs\node\22.23.3\node.exe` | `node-install.log`、`node-version.log` |
| npm | 10.9.9；desktop npm ci 安装287包成功 | `npm-version.log`、`npm-ci.log` |
| TypeScript | `npm.cmd --prefix desktop run build` 退出0 | `build.log` |
| Electron | 锁定44.9.0，显式运行 install.js 后 exe --version 通过 | `electron-install.log`、`electron-version.log` |
| 三个纯测试文件 | browser-policy/oauth-return/ui-preferences：5/5通过，无skip | `pure-tests.log` |

mise绝对路径：`C:\Users\plumlocal\AppData\Local\Microsoft\WinGet\Packages\jdx.mise_Microsoft.Winget.Source_8wekyb3d8bbwe\mise\bin\mise.exe`。命令经该mise的 `exec node@22.23.3 -- ...` 执行，不依赖全局Node24。Electron44内嵌Node24.22.0/Chromium152.0.7977.167属于锁定Electron自身运行时，与宿主构建Node pin分开记录。

## G0-W-N：12个隔离可行性子项通过，整门仍待验证

有限实验源是 `tls-electron.cjs`、`tls-guest.py`，生产程序不会import它们。Electron使用维护方Session/Chromium TLS验证、webRequest、BrowserWindow、EventSource、DownloadItem扩展点；guest仅用Python标准库TLS/HTTP fixture和本机OpenSSL生成证书。Pulsara探针只比较经可信私有stdout pipe交付的完整public cert，并限制精确origin；不替换TLS实现、不建立proxy、不改trust store或webSecurity、不忽略所有证书。

已读锁定 `desktop/node_modules/electron/electron.d.ts` 对setCertificateVerifyProc缓存、closeAllConnections、fetch/download的定义，并核[官方Session文档](https://www.electronjs.org/docs/latest/api/session)及[DownloadItem文档](https://www.electronjs.org/docs/latest/api/download-item)。证书callback没有port，独立webRequest精确限制scheme/host/port；每次新launch分配新内存Session并实际更换WebContents。

最终一次实际运行：Windows Electron PID10476，wsl.exe carrier PID33452，Linux fixture PID7959/uid1000；`https://127.0.0.1:40021`，2026-10-24T15:28:06Z至15:28:11Z。guest使用 `/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python`（root准备的3.12.15）。三个TLS key/cert只在0700的guest临时目录生成，0600文件经SSLContext加载后整个临时目录删除，key从未经host pipe或日志输出。

1. Windows Chromium Session.fetch经WSL localhost获取中文正文，完整public certificate精确匹配。
2. HTTP降级、localhost别名及邻近port在网络请求准入阶段被拒绝。
3. sandbox/contextIsolation/nodeIntegration:false renderer中require、process和pulsaraDesktop均不存在。
4. 关闭旧连接后EventSource新连接收到两个中文SSE事件。
5. sandbox iframe加载，父页面不能读取其document；image/PDF端点MIME/字节经同Session读取。
6. webContents.downloadURL经DownloadItem保存真实Windows文件，逐字节等于中文UTF-8加0..255的fixture载荷。
7. fixture服务对错误Host返回421、错误Origin返回403、真实Chromium计算的cross-site Fetch-Site返回403；拒绝响应与请求header均保留。
8. 同port替换成错误certificate的新TLS服务后，旧Session请求失败，服务未收到受测HTTP请求。
9. 第二launch分配不同Session，旧WebContents实际destroyed，新WebContents装配第二证书后成功；退休旧Session拒绝请求。
10. 退休旧Session的4个并发重连请求均在准入拒绝，没有抵达guest。
11. 第二launch同port重绑定为第一launch旧cert，第二Session拒绝误连。
12. 新Session对完整精确pin的过期证书仍成功：certificate validFrom 2020-01-01、validTo 2020-01-02；没有修改系统时间或trust store。这证明此SDK扩展点可将本次精确身份与默认证书有效期处理分开，尚未证明跨真实时间变化/长期运行全部行为。

最终：`tls-results.json` summary12/12；`tls-electron.log`完整运行日志；`tls-guest-events.jsonl`流式事实；`download-中文.bin`实际文件。carrier code0/signal null，guest发stopped，guest key临时目录不存在。退出后只读扫描未发现本次TLS fixture Linux进程，也未发现仓库Electron进程。

限制：这是fixture可行性，不是生产aiohttp/TLS/preview owner接通证据；PDF只检查MIME及字节，不宣称完整PDF可视渲染。尚未覆盖时间变化/长期SSE、并发retire时所有inflight下载/上传/preview闭包、系统OAuth真实账号、VPN/代理/firewall变化和睡眠恢复。Null-Origin HTML的完整生产路由合同也未验证。整G0-N、Windows支持和B均未通过。

## 实际BrowserOwner的11个Windows子项

`browser-owner-probe.cjs`是现有 `desktop/scripts/browser-owner-smoke.cjs` 的隔离副本，仅将其production dist require和证据输出改成本次明确位置；加载实际 `desktop/dist/browser-owner`，未修改生产smoke。

11/11行为断言通过：无bridge/sandbox、隐藏保状态、Session共享/隔离、导航/POST/history、stop/retry、权限拒绝/popup、control-port对navigation/fetch/WebSocket/worker阻断、退休tabless worker及fresh partition、renderer crash、host reload、host/window销毁。证据 `browser-owner/results.json` 与 `browser-owner.log`。预期unsafe-port拒绝日志保留。

原smoke最后在Electron仍持profile文件时rmSync遇Windows EPERM，随后进程退出0但发生UnhandledPromiseRejection。因此行为11/11不等于该原smoke具备Windows干净收尾资格。确切留下的目录：`C:\Users\plumlocal\AppData\Local\Temp\pulsara-owner-test-p7z6Dx`。可由用户在确认该fixture已退出后，用Explorer定位并手工仅删除这个目录；本实验不继续删除它。

自动审批审查拒绝了退出后对上述已核验精确Temp子目录执行PowerShell递归删除，返回理由仅为 `blocked by policy`；命令没有执行，没有换工具绕过。

## G0-W-F：Explorer子项通过，整门仍待验证

使用computer-use技能实际启动Explorer，图形导航到 `\\wsl.localhost\Ubuntu\home\plumwsl\workspaces`，通过图形新建目录操作建立并打开 `中文 工程 G0-20261024`。没有用终端mkdir代替。GUI观察为空目录，保存bounded窗口截图 `explorer-unc-created.png` 和当时UI树 `explorer-unc-tree.txt`。

随后guest只读观察得到同一canonical目标 `/home/plumwsl/workspaces/中文 工程 G0-20261024`，uid1000、mode0755、空目录，`findmnt`显示 `/ /dev/sdd ext4`。事实在 `explorer-guest-observation.json`。该空目录作为明确实验产物保留，不删除；只关闭本次新建的Explorer窗口，不操作用户其他窗口。

此项仅证明Explorer UNC图形新建与Linux canonical/ext4观察相符。无active session的Electron有限目录服务/picker、跨发行版/DrvFs/bind mount拒绝、权限/替换/取消/丢回复、Windows File原始字节导入、另存失败/unknown均待验证，不能勾整个F。

## 失败证据与清理边界

首个TLS试跑因OpenSSL3.5拒绝 `-days -1` 在fixture生成期失败，保留 `tls-results-first-failure.json`、`tls-electron-first-failure.log` 及stderr；仅删除了当时精确已归属的Linux临时证书目录，后续加入atexit清理并改用支持的-not_before/-not_after。第二次因Electron默认最后窗口关闭导致提前退出，日志保留 `tls-electron-second-incomplete.log`。第三次实测发现Chromium不能通过自设Sec-Fetch-Site伪造该保留header，11/12失败保留在 `tls-results-third-failure.json` 和对应日志；随后改为真实跨origin请求并同时验证fixture响应，未放松断言或跳过场景。最终12/12结果独立保存。

本次没有终止/重启/注销发行版，没有修改WSL、系统网络、用户设置、OAuth或用户工作。下载、TLS profile、报告/探针均保留于本次output目录。仅有上述BrowserOwner临时profile清理被审批阻止，已明确披露。
