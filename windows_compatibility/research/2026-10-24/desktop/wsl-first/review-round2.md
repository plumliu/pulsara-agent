# WSL-only根规格：最终语义候选全文复核

日期：2026-10-24。对象：`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md` 394行最终语义候选全文。已分段重新通读全部正文，结合第一轮源码事实与terminal/backend提案核对。未改根规格/生产/保存配置，未运行测试、G0-W、构建、provider或数据库操作。

**结论：D1已关闭；可冻结，无剩余阻断。** 此批准仅指实施合同；A/B未实施、G0-W未执行、Windows未验收/激活的事实保持。

## 1. D1关闭依据

根§3.2已列出真实受影响位置：`desktop_app/application.py`、`conversation_kernel/host.py`、`capability/mcp_management.py`、`desktop_runtime.py`与Node `runtime.ts`。§10.3明确复用现management async `open_browser`注入点，经DesktopApplication/KernelHostCore一路贯穿；不会保留guest `webbrowser.open`却声称能在interop禁用/无WSLg情况下自动打开Windows浏览器。

唯一机制明确为原可信JSONL的有限`open-oauth-browser`请求/结果，使用本次requestId和已验证HTTP(S)授权URL；Node仅当前live launch验证无userinfo/正确scheme，由main有限`shell.openExternal`动作回真实结果。原listener先启动、SDK PKCE/state/issuer与token继续由原owner裁决，不把打开成功当authorized；code/token/clientsecret不进入宿主通道。

取消、retire和late请求拒绝，已发请求后的取消不假称浏览器被回滚；pending future/listener沿原owner清理，无自动重复打开、通用反向RPC或durable任务。G0负测覆盖PKCE S256/state/issuer/一次性callback及Windows转发/端口冲突/rebind。这个接缝最小且能具体实施，第一轮D1已关闭。

## 2. 最终新增条款复核

- **PG有限调用**：NB现kernel flock→ready→Node最终取消检查→唯一operation；ready前取消结束stdin并join实际helper，不仅reject Promise。已发则承认副作用不可回滚，锁覆盖真实命令/readback。实际helper先死而child仍活会让闸门失败，没有用status3或文本持锁作假保证。锁CLOEXEC避免常驻PG持锁；没有添加新锁/操作registry/持久ack。
- **wait-owner**：§9.1明确直接原desktop_runtime或包内独立模式，不能让PG helper先持锁再嵌套取同一锁。等待仅串行屏障，后续PG操作仍自己取锁，避免自锁或把观察当授权。
- **关闭结果**：正常stopped/无failed/Linuxexit0/完整pipe与carrierclose分别判断；attempt失败/unknown留原runtime过程字段供stop/quit检查，child清空不升级成功。不凭kernel锁释放宣称所有副作用成功，不提前停PG/覆盖资源。
- **TLS长期运行**：短命指本次owner生命周期，未引入证书到期的任务总寿命cap；时间跳变、长连接和pin时间处理有G0要求，不能到期重启kernel/换epoch。每launch新Session+真实Window/WebContents替换，旧Browser/StartupCover/IPC/stream/upload/download完整retire，未退回hot-swap pin。
- **资源与发布**：native Ubuntu构建guest Linux制品，Windowsrunner仅Electron/NSIS及组合资源；uv构建Python不被误当Windowskernel。rootfs/payload/host输入与cache分别声明，新leaf及时失效。release原owner增加精确`Pulsara-${version}-windows-x64.exe`，保留DMG/两个deb、manual workflow及publish-never验证语义。

## 3. 全文相容性结论

1. **单一路线与A边界**：root完整替换旧native目标、G0与宽A，未残留Win32/ConPTY/PS/NTFS全消费者迁移前置；收窄A为真实host/PG/locator接缝，可独立交付，G0-W仅控制B。专用Pulsara Ubuntu24.04/nonroot、禁interop/automount、无用户distro/global config修改、Linux工具链语义相互一致。
2. **所有权与最小机制**：main/runtime/BundledPostgres各保一份状态，Linuxhelper仅有限物理事实；role/schema/kernel/terminal/MCP/hooks/文件机制原样继续Linux。Unix PG/Protocol-v3/diagnostics不对Windows改TCP，不增secret文件/manifestauthority/daemon/恢复框架。
3. **图形工作区与文件闭环**：唯一user-level Linuxpicker无session可新建/浏览/选择，root内ext4/no-follow/mount检查并在session admission重验；Explorer手动管理与UNC文件引用入口各有明确作用。Windows文件标准File输入/上传是副本，目录不隐式递归，Linuxexport与Windows保存为两个动作；preview仍受token/root/CSP约束，普通Browser仍拒控制port/file协议。
4. **部署和CLI**：官方固定rootfs、离线传入、有限rootprovision与日常nonroot分开；同名未知/部分失败不认领重建。更新/普通卸载保数据，NSIS不能强杀后覆盖。首版不做Windows全局consoleCLI，WSL内app明确提示Windows图标，不开interop/反向常驻服务。
5. **验证和成本**：A按实际影响执行Mac/nativeUbuntu层，缺GUI/native不得算完成；G0-W与B另需真实Windows、跨域控制/PG/TLS/GUI/故障/provider长程，不拿Mac/容器/mock替代。50–95人日统一分配、约63–129含余量，未叠旧native和各专题重复成本。保存配置/secret边界、provider前缀、原oracle/容量/长期可用性均保留。

G0仍有需实际证明的集成风险，特别是carrier/有限helper生命周期、TLS缓存及窗口更换、rootfs供应链与失联行为；正文把这些设为有硬退出条件的闸门，没有假称本轮已通过。root如后续修改这些执行/安全/产品语义，须重新审阅；仅冻结元数据更新无需重跑本次研究/测试。

## 最终394行候选的两处精确修订确认

已复核§3.3与§10.3：所有operation帧前取消（含ready后尚未发帧）都结束stdin并join实际helper，未遗漏持锁但无操作帧的等待者；OAuth打开sink只拒本地已知close/retire，跨eventloop尚未知的Linux取消不虚构浏览器撤销保证，原flow/pending关闭并拒晚结果/回调、不自动再开、不新增cancel registry。两处都使物理副作用/取消责任更准确，与前述批准相容。

**同意此394行最终语义候选冻结，无剩余阻断；D1仍关闭。** 后续仅冻结元数据更新不改变本批准。本次未重做研究或执行测试/G0/构建/provider。
