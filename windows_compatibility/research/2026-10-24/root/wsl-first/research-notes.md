# WSL2主路线修订：root汇总与独立复核

日期2026-10-24。用户明确将优先路线改为Windows Electron+WSL2全内核，要求重新讨论、收窄A、补图形工作区/路径/部署/生命周期并交叉审阅冻结。本轮仅文档，不运行生产/G0/WSL/provider或数据库操作。

## 授权与范围

- 复用三位gpt-6.1-sol/high agent，在各自wsl-first目录写提案/审阅；root唯一改根spec。此前695行native/宽A冻结被新路线完整取代，快照仅供追溯。
- 当前选择固定Pulsara Ubuntu24.04 WSL2与nonroot pulsara；专用distro避免改用户已有Ubuntu的interop/mount/工具状态。interop false与PATH false分开，automount关闭；不把它当安全沙箱。
- 选择用户级Linux-backed workspace picker作为唯一主流程（backend与root建议），未采纳desktop最初UNC主picker建议。Explorer仍可手动新建/复制/reveal；原生UNC仅Linux文件/目录引用。C盘文件明确File副本导入，不隐式目录递归/同步。
- 首版不做Windows全局consoleCLI，Linux app不借interop开WindowsGUI；明确提示Windows图标。此项与desktop原提案不同，最终根spec单一路径有明确边界。

## 源码复核及关键取舍

已读取AGENTS、main/runtime/postgres/preload/browser-policy/desktop-host、workspace_identity/path_completion、prompt-draft/file-drop/runtime-adapter、file_import/HTTP/preview、desktop_runtime、MCP management/host/OAuth与构建/release配置。检查Node fs与Linux路径/uid/mode不能等价、PG启动与start-frame顺序、finally stopped/failed/锁释放、runtime child清空后stop提前return、BrowserOwner控制port隔离。

- PG最初agent对“迁Linux owner”有分歧。经直接讨论收敛保留TS BundledPostgres唯一lifecycle，Linuxfinite helper只执行物理命令/文件事实，不新daemon/PG状态或DDL owner。
- backend发现lostreply后status=3可能旧start仍inflight；复用原kernel flock覆盖helper物理执行/readback，CLOEXEC不传PGserver。root进一步指出旧helper尚未取得锁的latepayload空窗；三人认可ready-before-operation现start-frame模式，Node取消在真实发送sink检查，EOF前无帧无副作用，发帧后持锁join不报未发生。
- terminal指出stopped与锁单独不代表成功，现stop child已无直接return可让carrier异常升级clean success；文档要求原process-local attempt保存失败/unknown，无收尾failed+正常exit0/完整控制生命周期才能正常维护。
- 桌面指出一次明文HTTP挑战无法保护后续rebind；采用SDK标准TLS候选，pipe传完整public cert，fresh workbench Session+exact origin，main实际替换Window/WebContents并retire全部旧owner。证书缓存无盲目clearAuthCache假设；临时cert不增加任务寿命cap。集成未证实均为G0-W硬门，不写成已实现。
- desktop第一轮唯一阻断D1：OAuth默认webbrowser.open在guest，禁interop无WSLg打不开Windows浏览器。root核现async open_browser扩展点后补DesktopApplication→KernelHostCore.production/ctor→LocalMcpManagementService注入，原JSONL有限OAuth URL请求/结果，Node仅当前launch打开Windows系统浏览器；SDK PKCE/state/iss/listener/token原owner不变，不造OAuth协议代理。

## 官方研究

读取Microsoft WSL配置/安装/命令/interop/filesystem权限/网络、WSL boot relay、Electron Session证书验证及缓存、aiohttp TCPSite SSLContext、Canonical rootfs定制和再分发政策。具体来源近旁写入根spec。它们证明API/平台事实，不是Pulsara原生G0通过；版本/包/许可与安装产物仍需后续实际验证。

## 预算

根统一50–95人日，含专用rootfs、窄A、宿主PG/TLS/GUI/部署/安装及一次完整联验；另25–35% reserve约63–129。不是把backend净15–25、desktop整体48–86与terminal重复相加，也不叠旧native88–154/旧WSL35–65+15–30。A4–8为当前实际边界估计，不再包含全typedfd/scope重构。

## 候选与审阅

首轮根387行。terminal/backend全文交叉核对另两提案后无阻断，desktop报告D1。root闭合D1与精确化：helper取消/独立wait模式、TLS长程/时间、构建目标、OAuth实际注入链。394行唯一最终语义经三位全文二审，并对PG全帧前取消/OAuth跨loop取消最终两处再复核；均明确同意冻结，无剩余阻断。root独立认可后仅更新文首/§15元数据，最终401行。流程已完成，见freeze-record.md；不以结束/沉默/此前批准代替。

检查章节1–15、代码fence、实际literal test文件存在与active原生前置退出；无源码/文档/证据SHA。Git可见只有未跟踪根spec，output研究资料按仓库忽略管理；未stage/commit。
