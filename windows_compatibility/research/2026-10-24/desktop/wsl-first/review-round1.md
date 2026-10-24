# WSL-only根规格：桌面全文交叉审阅第一轮

日期：2026-10-24。审阅起点：root发出的387行WSL-only候选全文，并读terminal/backend的wsl-first/proposal。另只读复核production中MCP browser扩展点。未改根规格/生产/配置，未运行测试/G0/构建/provider。当前根文件正由root闭合各专题反馈，本记录不提前批准尚未复核的最终语义。

**第一轮结论：有1处需闭合的具体接缝阻断，最小修法如下。其余已审范围无阻断。**

## D1：OAuth系统浏览器缺少实际Linux→Windows调用接缝

位置：根§3.2与§10.3；生产`capability/mcp_management.py:329,342–349`、`conversation_kernel/host.py:7877`、`desktop_app/application.py:91`、`conversation_kernel/mcp/oauth.py:515–532`。

当前management默认调用guest中的`webbrowser.open`；KernelHostCore生产构造没有注入其现成`open_browser`扩展点。WSL本路线关闭interop、无WSLg且Windows系统浏览器由host拥有，故“由受限host动作打开”不能直接复用当前默认实现。SDK中的PKCE/state/issuer保证不能替代真实Windows浏览器打开链。

最小修法：复用`LocalMcpManagementService(open_browser=...)`已有注入点，在WSL composition经DesktopApplication/KernelHostCore贯穿一个OAuth-only callback；明确采用原可信JSONL中有限的open请求/实际结果回送（或选择一个同等窄现有扩展点，最终唯一机制），由当前live launch的Node验证HTTP(S)/无userinfo后调用`shell.openExternal`。打开结果/失败回到原OAuthflow，取消/retire后拒绝；不传code/token，不把“系统浏览器打开成功”等同授权成功，不引入反向常驻RPC、通用宿主命令服务或新的OAuth owner。Mac/Linux继续现注入默认，不开启interop补缝。

G0应覆盖：listener先启动、Windows浏览器实际打开、取消与retire、错误/过期请求、端口冲突/转发/rebind、SDK state/issuer/PKCE与一次性回调；错服务不能标authorized。backend已核实锁定SDK的S256/verifier/state及原callback扩展点，可直接引用事实，不复制协议。

此项是调用接缝缺失，不要求现在执行G0或新增部署前置；根补清后可复核关闭。

## 已核对且不阻断的内容

- **路线hard cut**：旧Win32/PS/ConPTY/HANDLE/原生PG/宽A全量移植已从当前根目标、目录、G0、预算、验收撤出；没有仅追加WSL却保留相反前置。固定专用Pulsara Ubuntu24.04 x64/nonroot，interop/WindowsPATH/automount的执行域边界明确，未宣称对同用户/root安全沙箱。
- **收窄A**：Node locator真实caller、host stream/finite exec与PG文件操作均接原main/runtime/postgres；不建立空Windowsleaf、统一VFS/ProcessScope/framework。A独立验收不依赖G0-W，Mac/Linux实际受影响native/installed验证；未变边界不强制重跑旧宽A长程全部矩阵，实际副作用/内核变化仍按现合同补测。
- **PG单一owner**：唯一TS BundledPostgres状态，Linux有限helper只给物理事实，不给其第二份startedHere/重试/DDL权。Nodefs不解释Linuxhome/Unixsocket，PG不暴露WindowsTCP。失联不能靠瞬时status3；现kernel flock+NB busy+ready-before-operation+Node最终取消检查使晚到有限调用有具体闸门。锁CLOEXEC不泄漏给常驻PG；helper先死而实际命令仍活的故障若破证明，G0必须失败修机制，未假称已解决所有OS故障。
- **关闭**：Linuxstopped单帧、kernel锁释放、wslcarrierclose与Linuxexit0分开；保持attempt失败/unknown以修复stop在child清空后假成功的实际缺口。只有真实收尾结果可停PG/维护/reset，不设强杀总预算，不把外部terminate用作正常cleanup。
- **图形workspace闭环**：唯一Linux-backed user-level picker可无session浏览/新建/选择；目录根与ext4/mount/admission明确；原canonicalID/availability不变。没有把session path-candidates当全局服务，创建目录/会话失败不会误删用户目录。UI叶名的Windows可表达要求不整体禁Linux工具已有文件名。
- **文件来源**：LinuxUNC引用入口与WindowsFile上传分开；当前Promise<string[]>不冒充File。Windows目录不伪装单文件递归import；Explorer提供明确手动复制/管理；Windows保存与Linuxexport是两个用户动作，取消/partial/unknown与覆盖有具体结算。
- **preview/browser**：现read-token、reading-root、HTTP(S)内容/资源、CSP/iframe保留；WindowsChromium不load Linuxfile URL；普通Browser继续拒控制整port及file协议。open/reveal是显式OShandoff，不假称fd身份穿过UNC继续冻结。
- **TLS每连接身份**：选标准TLS而非一次明文challenge；经可信stdio给public cert和exactorigin。Electron验证callback缺port且有cache，所以exact-origin admission和freshSession均必需；main替换真实Window/WebContents并retire旧BrowserOwner/StartupCover/IPC/流/下载，不只替session变量。G0硬验错端口/旧cert/WSL重启/新连接/iframe/null-Origin，失败不放宽/造proxy。该方案尚未证明通过，明确作为可行性闸门。
- **部署/维护/CLI**：rootfs固定来源/版本/内容验证，离线import与WSL engine前提分开；专用distro unknown同名不认领。应用资源与数据VHD分开，更新/普通卸载保Linux数据，不unregister假恢复；NSIS改现customCheckAppRunning避免默认强杀。无Windows全局CLI；WSL内CLI的app提示Windows图标，不开interop/反向daemon。
- **成本/证据**：50–95人日分包加25–35%余量约63–129，算术一致；不叠原native预算或三专题重叠估计。新增用户级picker/TLS/session/distro维护已入统一表。规格冻结/A/G0/Windows激活四状态区分，旧provider与baseline runner限制保留。

## 后续复核范围

root闭合D1及其他专题反馈后，复核唯一最终候选全文/语义变更再明确“可冻结/阻断”；第一轮没有用无人反对或G0未执行推定批准。
