# Windows G0 文件与真实 HTTP owner 集成

2026-10-25；受测生产基线 `b741dd92b06a2d167d882e312e18f503e23b8a89`。本轮仅改 output 实验探针，未改生产代码，未进入 Phase B。用户已将原生 Ubuntu 资格移出本轮范围；它仍待验收，未被 WSL 替代。

Windows Electron 44.9.0；已有 Ubuntu / plumwsl UID1000；WSL nat，Hiddify TUN 的 `tun0 / sing-tun Tunnel` 实际 Up，interop 保持。`network-adapters.json` 与 `firewall-observation.json` 记录原状：Domain/Private firewall enabled，Public disabled；未修改任何网络或安全设置。结论仅覆盖这个实际环境，不能推广成所有 VPN/firewall 配置通过。

## 图形目录与真实 Windows 文件输入

`files_host.cjs` 创建独立 Windows Electron Session/窗口，完整公钥证书及精确 origin 来自本次 WSL 继承 pipe；没有 preload、Node integration、active session。`files_guest.py` 的有限目录研究服务复用 production no-follow directory binding、workspace resolver；导入复用 `LocalImport`。这个小服务不是正式 setup/picker/HTTP 权限接入。

实际通过 Computer Use 在 Windows 窗口完成：浏览用户 workspaces，输入并新建 `中文 工程 G0-R5`，进入并选择这个目录。guest 返回同一 ext4 canonical 路径、原 workspace key、`activeSession:null`。最后取消选择显示 `cancelled:true/activeSession:null`，见 gui-final-tree.txt。取消不提交目录写入或创建会话。

Windows 原生文件对话框已实际打开，但自动化工具两次无法绑定其 modal（not target window）；由用户现场选择本次合成的 `Windows 中文附件.bin`。用户随后现场完成 Explorer→虚线框拖放。guest 共收到三个独立副本，全部逐字节比对原始285字节：UID1000、文件0400、目录0500、ext4 /dev/sdd。`gui-final-import-verification.json` 保存三份路径与观察；没有记录每次 DOM 输入事件ID，不能给三次服务端写入各自编造精确 chooser/drop 对应。原单次 chooser 证据保留在 gui-import-verification.json。

GUI初版漏用现 `frontend/lib/runtime-adapter.ts::importFiles` 的 ASCII JSON filename 编码，中文 header 在浏览器内就抛 ByteString 错误；当时未发送上传。修正仅复用该编码表达式，不改生产。失败原记录 files-unicode-header-failure.json 保留；files-gui-v2.py 是受测 GUI 源码快照。

## 路径与副作用边界

`files-matrix.json`：17项真实 Windows HTTPS→Linux 请求通过预期断言：canonical及两个同发行版 UNC别名、其他发行版/C盘/网络UNC/DrvFs/越界拒绝、ext4及DrvFs链接拒绝、无权限/缺目录/重名/分隔符/Windows保留名拒绝、mkdir已发送但丢响应后刷新确认存在而不重放、部分HTTP上传断连后无临时stage/已发布残片。

`files-races.json`：两个确定性真实命名空间竞态，各保留原反例及候选正证。

- 持有父目录fd之后，将该父目录改名并放入替代目录。真实 mkdir 写在旧held父目录，路径重核失败。初探针普通 FileNotFoundError 不能表示“未执行”；最终候选返回 unknown，保已产生目录、不重试、不删除用户目录。
- 原 resolver 前替换所选目录。初探针会接纳替换目标；最终候选在接纳前重新bind并核与原held fd同一inode，拒绝变更。

`files-mounts.json`：在本次独立、private propagation的mount namespace内，root仅建立两个实验bind mount，随后实际降到UID/GID1000再调用接纳检查。一项把DrvFs挂到Linux-looking路径，一项把另一个ext4目录bind到目标，两者均拒绝。namespace退出后在新的非root进程确认两个目标恢复普通根ext4挂载，再清确切实验目录。没有改变共享Ubuntu的挂载表或用户目录。

交叉审阅指出空组件/`.`会被Path归一化；最终候选明确拒绝这些歧义输入。`directory-final.json`在最终源码上额外通过9项真实HTTP检查，包括正常list/select/mkdir/新目录select、重名与4项Linux/UNC歧义输入，carrier exit0及本次home/workspace清理。上述分阶段证据没有宣称所有早期GUI场景在最终源码整套重跑。

## 实际 production HTTP/import/preview owner

`http_guest.py`直接使用 `LocalHttpServer`、`RendererBridge`、`LocalImport`、`FilePreviewHttp`、`FilePreviews`。仅将现aiohttp TCPSite注入SSLContext，并在研究子类中将origin改为HTTPS；HTTP路由/错误/security/streaming/descriptor/token实现不复制。session/controller配置使用现测试方式预置SimpleNamespace；不伪称完整production会话创建或main装配。

最终 `http-results.json` 的13个选定场景通过：真实Origin/Host拒绝、import角色与generation拒绝、文件逐字节导入、文本与跨connection token拒绝、HTML sandbox/null-Origin同token资源、图片解码、PDF、下载、保存失败、文件替换失效、preview revoke及旧Session拒绝并发新请求。Linux生产原文件测试另跑17 passed，9条现aiohttp deprecation warnings保留于production-file-tests.log。

PDF不能仅靠MIME/字节证明显示：第一版精确后端origin过滤会挡Electron内置PDF extension，出现白屏；仅放extension仍挡其chrome://resources脚本。最终只允许锁定Electron实际内置PDF extension ID，以及仅由该extension frame请求的chrome://resources本地资源；所有后端网络请求仍须精确HTTPS origin及完整certificate。窗口实际显示单页 `G0 Windows WSL2 PDF`，root已查看pdf-render.png。隐藏窗口截图仍白屏，不能冒充可见窗口验收；最终正证来自可见窗口。

PDF内置资源依据：Electron v44.9.0 [component extension resource manager](https://raw.githubusercontent.com/electron/electron/v44.9.0/shell/browser/extensions/electron_component_extension_resource_manager.cc)。生产接入还须将本地UI资源与backend网络准入明确区分，不能放宽任意chrome-extension/file/外部HTTP。

保存目标为已有sentinel文件下的子路径时，Electron给可恢复 interrupted，而非立即done。第一次等待done超时的原记录保留。正确按[DownloadItem生命周期](https://www.electronjs.org/docs/latest/api/download-item)观察updated interrupted，然后显式取消本次失败下载、等待done cancelled；原sentinel完整且未重试。同步在updated回调内cancel触发native observer reentrancy日志；移到setImmediate离开当前回调后取消，最终宿主exit0且stderr为空。没有用cancel把下载冒充完成。

## 原失败与退出事实

- 首次GUI用PowerShell Hidden启动，没有可用窗口；核精确可执行路径/命令后结束该自有host，保存files-hidden-process.json及已取得的guest事实，后续正常可见启动。它不算GUI正证。
- 首次HTTP实验由PowerShell直接启动GUI exe，父shell提前结束日志pipe，console.log抛EPIPE并出现用户截图弹窗。核精确host后收回，改直接写本次log；http-pipe-launch-failure.json保留。错误来自研究启动器，不是生产app错误。
- 初Origin阴性被Chromium CORS预检先挡（OPTIONS405），不是实际POST403。最终使用request header观察接缝让真实POST到原middleware，记录method=POST/status403；原失败未改写。
- PDF初白屏、同步download.cancel native日志、早期失败矩阵与源码快照均保留。最终http-host-final-exit.json为33920/exit0，http-host-final.stderr.log为空，13/13；不是以中间打印PASS代替截图/进程退出事实。

## 清理与范围

GUI最后guest stopped、carrier close0、host-stopped齐全。cleanup-files.json只删除10个明确观察到的本轮home；7个HTTP home核实验模板，3个文件home核ready记录。两次HTTP中间运行的ready文件被后续覆盖，报告如实区分缺单独ready快照与模板核对，未编造pid identity。扫描没有仍运行的对应Python server源入口；Windows精确实验命令无残留。readonly imports先恢复自有目录写权限再删，另清自有中文实验workspace；未触首轮保留的中文目录或此前被自动审批拒绝清理的BrowserOwner目录。

这些证据闭合所列F机制/图形输入与N真实HTTP子项。持续SSE/慢上传下载在途退休、自然证书跨有效期、真实睡眠恢复、真实Notion OAuth和当前kernel/main控制链另由复用agents继续验证，不能凭这13项勾完整G0-N。正式picker/controller接入、安装器、生产Windows TLS与跨域scope均仍属Phase B。
