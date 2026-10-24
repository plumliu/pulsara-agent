# 根规格全文交叉审阅 Round 2（desktop agent）

日期：2026-10-24。对象：根`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`修订版（本轮全文388行）。第1–15节已重新通读；本次审阅不修改根规格或生产实现。

## 最终结论

**可冻结。无剩余阻断。** 这是对“原生Windows 11 25H2 x64、本地NTFS、G0前置的条件实施规格”的明确认可，不是对G0已通过、Windows实现已完成或发行可用的认可。G0若无法满足半关闭、原生扩展或安全文件等规定语义，必须修订规格，不能自动fallback。

## 一审事项关闭确认

| 事项 | 修订位置 | 结论 |
| --- | --- | --- |
| A：默认NSIS可强杀已知app绕过settlement | §10生命周期及customCheckAppRunning段（274、280行附近） | 明确已知/未知进程都不能被安装器强杀后覆盖；用户正常Quit和资源owner收尾是前置条件；准确指定已锁builder现成宏extension替换安装/卸载默认kill；覆盖main已退但Python/PG/Chromium持资源的真实installer验收。关闭。 |
| B：custom OAuth URI权限歧义 | §9 OAuth（257行附近） | custom URI只focus，argv不接受code/grant；OAuth state/授权仍归原HTTP loopback owner，不交shell求值。保留当前exact return的focus-only语义，无新授权旁路。关闭。 |
| C：匿名stdio与UTF-8两个独立问题 | G0-R、§7.3（217行附近） | UTF-8无BOM字节wire、增量解码与独立UTF-8 stderr已冻结；真实Node→bundled Python匿名pipe、中文/emoji、拆chunk、shutdown/EOF/broken pipe/pre-ready/crash和reader join入G0；不改系统codepage/Selector或吞乱码。关闭。 |
| D1非法叶名测试混淆 | §6.3（175–177行附近） | 单反斜杠root-relative、合法叶名往返、非法?*<>|拒绝、URL query/fragment明确区分。关闭。 |
| D2原生window controls | §9（249–250行附近） | Electron hidden+titleBarOverlay和snap/系统菜单/DPI明确；Windows tray失败时保留可操作窗口，不hide失联。关闭。 |
| D3 CLI依赖复用 | §10（273、278行附近） | native console转发仅管理argv/stdio/exit/Ctrl-C；先检查维护中的launcher能力，缺口才窄适配；不以.cmd %*假定无损。关闭。 |

## 修订新增部分的跨专题复核

- **内部Protocol-v3**：§2/7明确它是会话桥而非仅terminal工具。Windows选择单一loopback stream + 原launch capability/帧/auth/attachment/settlement，精确listener lifetime、SO_EXCLUSIVEADDRUSE、close/connect/hello线性化与抢占拒绝入G0；不是把端口当identity。diagnostics仍是local-only SID DACL named pipe且验证server/home。三条IPC用途区分，未另造RPC框架。
- **数据库认证**：§7.2同时改Node PG owner、backend validator和typed DSN parser，client require_auth=scram-sha-256可拒绝错误认证挑战；auth材料的持久存在有TCP认证独立必要性，既有owner、私有ACL、发布/initdb/角色/reset失败路径、无自动轮换/清库均清楚。不是execution receipt或新的settings authority。
- **文件和包执行**：§6/8把handle安全、Windows source metadata N/A、真实launchability、显式解释器分开；不把POSIX执行位伪成全部允许，不扩大包格式/身份；preview/import/export等消费者全列出，前端路径不是授权。
- **非终端进程**：MCP自己的Windows inert env baseline、lookup_path、secret resolver和spawn guard明确，hooks保留自己的overlay/scrub；没有把terminal env policy强加给所有子程序，大小写key小机制可复用。
- **PowerShell/Job**：标准PowerShell -Command退出语义、长短命令同语义、调用方显式选择native数值退出码、无全局NonInteractive与EAP修改，减少自造shell语义；Job通知只唤醒，ActiveProcesses/root/drain/join分离，和桌面退出条件一致。真实可行性仍归G0，不是静态通过。
- **WSL备选**：整后端同域、配置/home、文件转换、localhost、Windows interop、distro所有权/卸载与wsl.exe不等价join写清；没有未经授权启动第二条产品路线。已有发行版不得静默改interop配置。
- **成本和证据**：W0–W7按唯一owner去重的88–154人日、reserve110–208、外部依赖/fork另估与日历条件合理；WSL不叠原生预算。Win11交互环境和Server runner分开，macOS24/49绿测不冒充Windows资格，Windows/真实provider未执行的限制如实保留。
- **仓库纪律**：prefix continuity仍仅cold/compaction successor边界；无新总寿命caps、durable事件/receipt/lease/执行恢复；用户级页面不依赖active session；只复用已核实依赖扩展点，明确拒绝参考仓库不适用的强杀/fail-open政策。未发现新增架构阻断。

同意root在其他agent二审阻断也关闭并独立认可后，把文首及§15改为“规格冻结，G0未执行”。该状态文字变化不需要重开实施语义审阅；若继续修改本次认可的执行/安全/认证/更新合同，则应再次审阅受影响部分。
