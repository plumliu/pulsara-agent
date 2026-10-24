# 根规格全文交叉审阅 Round 1（desktop agent）

日期：2026-10-24。对象：根`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`本轮初稿，全文第1–15节已通读，额外核对路径、后端/前端闭环、资源owner与总预算。没有修改根规格。结论：**有1项实施语义阻断及2项应在冻结前消歧的边界；修订后可二审。** G0条件冻结的方向、native目标与WSL不自动fallback均赞同。

## A. 阻断：安装器仍可能绕过正常结算杀掉“已知”应用

位置：§10生命周期表（本轮约249行），与§7.3普通退出（约200行）。

文案只写“不从安装器强杀未知进程”。electron-builder/NSIS默认运行中应用检查可能允许关闭已知主应用，当前句子没有禁止绕过Python shutdown→物理children join→child close→PG stop就开始覆盖。**已读取当前锁定builder26.15.3实际模板**：`desktop/node_modules/app-builder-lib/templates/nsis/include/allowOnlyOneInstallerInstance.nsh:37-41`提供`customCheckAppRunning`宏扩展；默认`:94`按安装路径前缀Stop-Process、`:127`先kill、`:140-141`睡1秒后force kill；`installSection.nsh:33-36`及`uninstaller.nsh:2`都会调用。不是假设性的未来硬化。建议使用该维护中的宏扩展点替换这一策略，保留其余NSIS引擎。

Windows安装资源被Python、Chromium、PG或terminal持有时，强杀主应用会被误判为可替换，破坏已确认的副作用结算和完整安装。

建议修法：明确**普通手动更新/卸载要求用户先完成正常Quit；发现同home应用或本次安装资源仍由受管理owner持有时，只允许等待用户正常退出/取消/具体失败，不能由安装器强杀正在结算的工作或以固定超时继续覆盖，包括已知Pulsara进程**。NSIS继续拥有安装/卸载引擎，不复制一套扫描/kill/install框架；所选include/config须用真实安装器证实不跳过此门。OS注销/强杀是§7.3单独的执行unknown，不能当普通更新完成证据。验收矩阵加入已知main退出但Python/PG/Chromium仍占用资源、安装取消、正常退出后再覆盖。

## B. 冻结前消歧：OAuth custom protocol仍只能focus

位置：§9 OAuth行（约232行）；当前生产`desktop/src/oauth-return.ts:1-5`、`main.ts:151-157`。

“pulsara自定义协议、第二实例argv、URL编码/来源/state校验”放在同一行，容易被实施者读成开始从deep-link/argv接受OAuth code/state。生产合同刻意禁止此路径：exact return URL只focus，grant/state归HTTP loopback + SDK OAuth owner。

建议明确：Windows cold/warm protocol launch只恢复/聚焦同一GUI；允许的exact return保持原合同；不得从argv/custom URL提取或提交code/grant/state。HTTP loopback授权仍由既有owner校验实际state/来源、处理失败取消；不得把协议URL交shell求值。无需新OAuth状态、持久receipt或nonce框架。

## C. 冻结前消歧：Node→Python宿主控制通道必须明确UTF-8

位置：G0-R、§7.3（约198行）；`desktop_runtime.py:337-346`与`desktop/src/runtime.ts:29-32,57-61`。

Python当前stdout`ensure_ascii=False`并默认text stream；Node readline默认按UTF-8解码。Windows继承匿名pipe的默认codepage与`connect_read_pipe(sys.stdin)`可用性是两个独立问题。只写“受控reader线程/实际adapter”还不足以避免中文错误或home路径损坏后被当invalid frame并退出。

建议冻结JSONL wire为UTF-8无BOM，日志stderr亦有明确解码合同。使用CPython/Node支持的encoding配置与受控字节pipe adapter，不能改系统codepage或全局Selector；G0-R通过真实Node spawn→bundled Python发送中文/emoji路径和错误，跨chunk拆UTF8、EOF、破管、pre-ready失败、正常shutdown→cleanup，并验证控制stdout不混日志。禁止以缩窄成ASCII或吞乱码替代产品诊断。

## D. 非阻断修订建议

1. §6.3非法字符测试将`?`与有效叶名字符放在一列。Windows原生文件名不能包含`?`，应区分合法`# % & ' ( )`往返、URL query/fragment解析、非法`? * < > |`叶名明确拒绝；不要要求创建不可能的NTFS文件名。
2. §9窗口建议明确优先Electron titleBarOverlay/原生controls，保留snap/系统菜单与DPI；如自绘增加相应验收。现有隐藏titlebar+仅Linux controls确定有缺口。不是要求新增任意window framework。
3. §10已选择native console launcher且有“先读维护能力”的约束，方向可接受；实施应先核实uv/Python打包console entry/成熟launcher的relocatable argv/stdio能力，避免开发一套参数parser。若选择自有native转发器，只处理转发、handle/exit/signal，不解释命令。预算W5已覆盖。
4. §7.3 named pipe诊断与root正在补的Protocol-v3 loopback是不同通道，必须保持唯一选择；不可统一成无认证loopback。root已声明会补，这里不重复列为未响应阻断。

## E. 跨专题与成本判断

- §5 terminal的creation-before-Job、raw bytes、完整join、长期任务与ConPTY close_stdin Gate跟桌面退出合同一致；半关闭若不可实现必须修订而非PIPE冒充TTY，条件冻结成立。
- §6保留安全file owner、preview token、no-clobber/replace分别定义，前端只接受受限动作，与现有frontend/preview闭环正确；UNC首批typed unavailable是具体产品范围，不能拖到各regex自行决定。
- §7 Windows PG TCP+SCRAM、双角色、私有凭据、reuse exact instance与backend validator一起改的闭合方向正确；端口不是身份、DSN不能变第二设置authority均保留。PG native可构建性未被假装通过。
- §8 MCP/Hook独立launcher与默认terminal PowerShell不混用合理；SDK已有Job支持但assign后race/fail-open不满足合同的说明准确。browser Playwright继续由依赖拥有版本，不把Electron guest与工具browser强行合并。
- §11 W0–W7加总88–154人日正确；资源/PG/runtime/OS原语/前端/验收已经按owner汇总，勿再叠三份agent报告总数。25–35% reserve得到约110–208人日的量级算术合理；2–3工程师12–22周属于关键路径与资源条件下规划，已有准确限制。此区间不吸收不可控的pg_search fork。
- §12 WSL35–65日+managed distro15–30日比桌面专题估计更保守，覆盖完整工具/配置/网络/路径/资源产品化可接受。不是native+WSL一起做的预算，无本轮实现授权。
- §13证据准确区分Win11桌面与Windows Server runner、G0与激活、真实provider配置只读、无新durability/总寿命caps；未看见为适配Windows暗改provider prefix的路径。

二审条件：A关闭，B/C明确写入；root承诺补internal Protocol v3与所有参考仓库具体结论后通读修订全文，再给最终“可冻结/阻断”结论。本审阅不声称Windows实现可用或G0通过。
