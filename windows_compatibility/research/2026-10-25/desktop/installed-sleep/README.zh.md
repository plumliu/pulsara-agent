# 正式安装版睡眠观察准备

root明确通知后已于2026-10-25 18:19:40+08采得正式beforeReady=true；原始before.json/before-run.log保留。用户手动睡眠并报告已唤醒后已采after，10项比较全部通过；506→507系统事件间隔79.9177558秒，4→6条TCP连接且旧端口均更换。完整事实与限制见report.zh.md。正式app/PG保持运行。脚本不启动/关闭kernel或PG，不设置网络/电源，不读settings、token、grant或浏览器authURL，不向应用发送业务写操作；不会直接让电脑睡眠。

仓库根PowerShell入口（端口由root届时确认；当前通知为40363）：

```powershell
& .venv/Scripts/python.exe -X utf8 output/phase-b/desktop/installed-sleep/observe.py before --port 40363
# root确认beforeReady后，请用户手动睡眠并唤醒；不要自动化睡眠。
& .venv/Scripts/python.exe -X utf8 output/phase-b/desktop/installed-sleep/observe.py after --port 40363
```

before/after原文件存在即拒绝覆盖。before如失败保留实际文件/错误，不重启应用来制造“同一实例”。after比较Windows主进程精确HANDLE creation、Linux kernel/PG PID+starttime/pidns/bootid、原control socket、两busy锁inode、原PG systemid/start/data、授权session/turn和同一HTTP listener。pidfd/HANDLE只在各次读观察期间持有，不冒称它们跨睡眠一直持有。

Linux侧先核HTTP监听socket确归该kernel，再读取其公证书；Windows只用这份完整公证书作CA与fullDER exact比对，GET /healthz一次，不重试，不换cert/origin。after仍使用before证书。Linux首个公证书读取仅是经实际listener归属核实后的公证书观察，不是替代生产BackendSession的证书准入；没有读取私钥。正式HTTP使用原health/long-poll，不能拿G0 SSE fixture冒称当前GUI原TCP全程不断。另记Pulsara进程的loopback连接前后，重连/连接变化需如实描述。

Windows只读System事件：Kernel-Power/Power-Troubleshooter的recordid/UTC/id/provider，不读取原Message/XML或外部浏览器进程。事件没有到达或不支持时报告缺项；用户自报时长与系统事件间隔分别记录，不伪造为指定秒数。观察单次I/O10s是测试读操作期限，不是产品寿命或睡眠总cap。

## 仍需用户在本应用内手动核验

- **Tray/后台**：点窗口X后应用仍运行，托盘“显示Pulsara”恢复同窗口/原session；如果图标在折叠区由用户展开。真正“退出Pulsara”入口应正常结算，root已有Ctrl+Q资格不能自动替代托盘入口。任何退出测试放在睡眠观察后或before之前完整结束，不破坏正在进行的OAuth。
- **中文IME**：在普通测试draft切换用户已有中文输入法，候选期间Enter只选字、不发送；Esc只取消composition；提交后再正常发送/取消一次，确认没有重复消息、误快捷键或光标错位。不得把jsdom合成事件当真实Windows输入法通过。
- **Ctrl/AltGr**：当前Windows标签与源码正确不等于物理按键已验。实际Ctrl+K开搜索、Esc关；Ctrl+N开新建后取消；Ctrl+Q已由root实测。若用户现有键盘布局有AltGr，输入该布局的@/€/{等字符并确认未被Ctrl快捷键误截；没有相应键盘/布局须标未验，不擅装布局或用普通Ctrl+Alt冒充。
- **DPI/snap**：用户在当前屏幕实际最大化/还原、拖动/snap，核标题按钮、输入光标、弹窗/预览命中。若已有不同缩放的第二屏，跨屏拖动观察布局、鼠标命中、HTML/image/PDF/native dialog；单屏当前scale不能冒称多DPI通过。本分支不会改用户显示设置，缺硬件条件就记录具体未覆盖组合。

以上不操作用户其他app，脚本准备也不构成这些人工门已通过。
