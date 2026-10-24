# Windows Electron + WSL2全内核规格冻结记录

日期：2026-10-24。根文件PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md。研究基线Git 60e8f21d37ca2c650500ae14438630006d3e62fb。

1. 用户明确选择WSL2全内核，root与原三位gpt-6.1-sol/high agent在各自wsl-first工作区重新讨论，完整取代旧native/宽A规格；旧695行快照仅研究追溯。
2. root独立读取生产/官方材料，汇总387行唯一候选后交三人全文交叉审阅。terminal/backend无阻断；desktop提出D1：实际OAuth默认webbrowser.open留guest，无Windows浏览器打开链。
3. root核源码并补DesktopApplication/KernelHostCore→management现async open_browser注入、可信JSONL有限open请求/真实结果；原SDK PKCE/state/iss/listener/token不变。并精确helper帧前取消/独立wait、TLS长程语义、分目标构建。最终394行实施语义。
4. 三位重读最终全文并交叉核对提案，分别review-round2.md明确可冻结。root进一步按非阻断意见精确PG帧前全部取消、OAuth跨loop取消不保证撤销；三位又读取并明确确认同一最终394行语义无阻断。
5. root逐份完整读取审阅记录，独立认可后仅更新文首/§15冻结元数据为401行。未修改已批准实施语义。文档章节/引用/旧前置检查见validation.json。

冻结决定：专用Pulsara Ubuntu24.04 WSL2/nonroot/禁interop与automount；Linux全内核/PG/工具；唯一TS PG状态+有限Linux物理helper；A仅Node真实host/locator/PG机制及POSIX回归；用户级Linux图形工作区；Linuxcanonical/UNC呈现、WindowsFile副本/另存；保数据rootfs/资源部署维护；正常/异常生命周期与结果unknown；本次TLS Session/Window退役；OAuth实际宿主打开链；G0-W与真实installed/provider/长程门；统一50–95人日估算（含窄A4–8），加不确定性约63–129。

root最终结论：可冻结，无剩余阻断。规格冻结不代表A/B/G0-W/Windows支持通过；本轮没有生产代码修改，没有部署WSL、构建、产品测试、provider、数据库或保存配置操作，没有stage/commit，没有源码/文档/证据hash。后续若改变本规格执行/安全/产品语义，须修订并重审。
