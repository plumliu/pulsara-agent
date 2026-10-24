# 根规格交叉审阅 Round 2（terminal agent）

审阅对象：`PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`，2026-10-24 第二轮唯一待冻结候选（约388行）。已重新通读全文，并单独完整读取第6–8节以避免长输出截断；本次覆盖终端、内部IPC、安全文件、数据库认证初始化、MCP/hooks/source launch、Electron/NSIS、成本、WSL与验收。未改根规格或生产实现。

## 最终结论

**可冻结。没有剩余阻断。** 此结论只表示实施规格的产品范围、所有权、失败规则、成本与验收条件可以冻结；**不表示G0已通过、Windows实现已完成或产品可激活**。根规格明确把这三个状态分开，符合本次研究交付边界。

## Round 1 问题闭合

| 项 | 当前文本 | 结论 |
|---|---|---|
| R1 internal Protocol-v3 | 7.3单独选择Windows loopback stream + 既有launch capability；三条IPC用途分开；exclusive socket、精确listener lifetime、connect/hello/close线性化、不向重用端口发送secret；W4/G0-R列入原机验证 | 已闭合。可复用asyncio stream与现有protobuf/auth/attachment owner，无新RPC/durableauthority |
| R2 PowerShell退出码 | 5.3明确标准PS7 -Command语义、显式exitN、没有native numeric-preserving额外policy、不全局追加exit LASTEXITCODE；长短command同bootstrap，G0验证parse/flow/exit | 已闭合。没有要求复制PowerShell控制流解释器 |
| R3 WSL interop | 12节明确Linuxkillpg不能证明Windowsexe退出，分别列interop.enabled和appendWindowsPath，禁止静默改既有distro；跨域owner另定并重估 | 已闭合。WSL仍是未授权的备选产品 |
| R4 Job emptiness | 5.2分开root/range/output/thread，IOCP只作唤醒，ActiveProcesses查询、handlewait限制和helper/conhost计数进入G0 | 已闭合。不提前释放容量或唤醒monitor |
| R5 NonInteractive | 5.3不为所有PIPE/TTY加-NonInteractive，保留Read-Host/长期输入；NoProfile与受控snapshot分开 | 已闭合 |
| R6 os.pathsep | 2.1已承认PATH分隔符原来可移植，问题在来源/shell/defaults/venv | 已闭合 |

## 其他全文复核

- native Windows11 25H2 x64/普通用户/本地NTFS是首批声明，不是任意任务寿命限制；WSL/GitBash/PS5.1/externalDB没有自动fallback授权。
- ConPTY/PIPE/Job只承担物理资源；现有ProcessRegistry、foreground decision、output/monitor、Host/childscope和side-effect settlement保持唯一owner。未增加持久事件、receipt、fingerprint、恢复日志、总轮次/调用数或总时限。
- PTY EOF仍是G0阻断项，未借关闭ConPTY或发CtrlD/CtrlZ伪装真实halfclose。若无法证明，必须先修订产品合同；设计冻结没有掩盖这一重要风险。
- Windows锁、no-follow/祖先替换、ACL、publish/no-clobber、settingsreplacement、借用删除与launch source属性都有真实API检查及原机失败注入要求，未把POSIXflag置0当支持。
- PG新增secret是SCRAM认证的独立必要输入，auth发布/initdb/角色初始化/reset失败顺序已明确；没有把跨文件/DDL操作假装原子，也没有偷偷造repair/recoverygraph。客户端require_auth避免凭endpoint误连伪服务。
- MCP自己的env/secret/lookup与terminalpolicy没有被合并；动态credentialguard、framebound、stdio/cancel保护保留。Windows文件执行属性、显式解释器、cmdwrapper与真实argv验证有明确边界，未靠shebang/X_OK伪造支持。
- Electron主进程仍有原生动作与PG所有权，renderer不能获得launchcapability/本机执行权；tray失败、sessionend、路径/附件/preview和无active session的用户页都有场景。
- NSIS默认强杀路径已通过现有customCheckAppRunning扩展点明确替换；安装器不得为覆盖资源牺牲现有任务结算，未复制整个安装框架。
- 88–154人日与不确定性后110–208人日的合计一致，说明上游长期fork不在其中；真实Windows硬件、签名、更新维护成本单列，WSL35–65及专用distro15–30没有与native叠加。估计没有被表述成完成承诺。
- 完整G0、三平台回归、真实provider/长程和安装包验收是激活条件；本轮macOS24/49测试只校验基线，没有被算成Windows通过。

## 冻结后须保持的执行纪律

G0选定维护库/受支持扩展后应按根规格写实际版本和窄接口缺口。尤其pywinpty公开spawn缺Job创建属性、PTY无公开halfclose/dispose的问题仍须原机证明或有窄扩展证据；这已经是明确工程gate，不是文档剩余歧义。实现者不能在G0失败后自行接受post-spawnJob竞态、uncontainedfallback、静默改PIPE/WSL、EOF假成功、依赖kill树扫表假装物理join或省掉pg_search。

最终批准语：**同意本候选按“实施规格已冻结、G0未执行、Windows未实现/未激活”发布研究结论。** 若此后发生语义修改，需要重新审查改动；仅更新审阅状态与排版不改变本结论。
