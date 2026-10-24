# 根实施规格交叉审阅：Backend 第一轮

审阅对象：根 `PULSARA_WINDOWS_COMPATIBILITY_IMPLEMENTATION_SPEC.zh.md`，2026-10-24汇总草案；已通读全文（包括终端、前端/安装和WSL）。结论：**方向正确，但以下三个阻断关闭前不可冻结**。root已告知将补Protocol-v3 Windows loopback、诊断named-pipe、pywin32 API与参考仓库明细，本轮不重复列这些已认领的缺项。

## R1-B1：新增数据库secret的首次初始化与失败路径未闭合

位置：§7.2，初稿188–190行；§7.3。

从private Unix socket转为TCP SCRAM确实需要独立认证材料，符合AGENTS的独立产品必要性。但“由owner产生/保存在私有运行配置，创建/轮换/删除限于owner”没有定义可执行的bootstrap/失败路径：配置文件与PG密码DDL不是同一事务，已有datadir但secret缺失、secret有但initdb失败、admin建立后runtime角色未建立等状态必须决定是否允许自动再建/轮换。自动生成新密码后无法打开旧PG很容易诱发危险reset/兼容恢复分支。

owner也写错了一部分：`desktop_runtime.py:75-112 prepare_database`创建/检查runtime role与database，`PostgresMigrationRunner`负责schema/grants/baseline而不是全体角色创建；`desktop/src/postgres.ts:78 resetConfirmed`负责明确确认后的物理datadir reset。规格不应让实现者把它们不经讨论揉进一个新DB manager。

具体修法：

- 在现有BundledPostgres owner内声明一个封闭、当前用户ACL的managed-DB auth配置，只承载该data-dir必需的admin/runtime材料；密码不进入argv/日志/终端env，必要时通过initdb支持的私有pwfile/等价受保护通道。
- 首次创建时先exclusive写出并flush必需auth材料，再以同一材料initdb；未完成初始化时拒绝覆盖非空未知datadir，不自动删除文件/重建集群。已有PG_VERSION但auth材料缺失/损坏/不匹配必须typed失败，不生成替代密码或自动清库。
- runtime角色初始化在已认证admin连接和现有`prepare_database` owner处完成；只在真正首次初始化创建；正常重开只校验/使用已存在材料与角色，不自动ALTER PASSWORD。
- 本轮无需扩展“密码轮换”产品。若要支持轮换，应另行说明两个存储边界失败时如何保持可恢复，而不是留给实现者自行创建修复图/receipt；最小方案可以明确不提供自动轮换。
- 明确确认后的物理reset可以删除该datadir对应的auth材料，只有精确本机目标与已关闭owner时才获准；schema migration/reset仍归现有runner。
- G0-D/最终tests加入上述每个失败点、错误实例认证拒绝，以及错误后绝不自动reset；不得只做happy-path连接成功。

## R1-B2：Windows包/Skill的可执行属性语义尚未封闭

位置：§6.2 sealing、§8（初稿214行“shebang、可执行位逐个校验”）。

`plugins/package_core.py:495-505`冻结的`FrozenPackageEntry.executable`直接由`st_mode & 0o111`派生；`:1040-1052`要求`./...`包内MCP command对应regular且executable。`capability/local_skill_publisher.py:885,904,1151,1305`也冻结/比较/恢复源执行位。Windows NTFS的ACL/文件类型/launcher与POSIX执行位、shebang不等价。仅说“逐个校验”不能阻止实现者将bool全改True、按所有suffix放行，或悄悄使native package永远不可启动。

具体修法：G0-F增加**Windows source metadata与实际launchability**探针，并要求在全面实施前将明确规则回填规格。可采用平台owner明确派生现有frozen executable属性与launch admission，不增加包格式/身份层；原生exe/com、需明确cmd wrapper的cmd/bat、需显式解释器的ps1/py/sh必须各自定性。非Windows脚本不得自动“翻译”shebang或把所有相对脚本交给pwsh。源metadata变化检测与安全冻结仍须保留，POSIX byte/ID输出不变；ACL sealing不能代替命令可执行性。支持/拒绝的native配置应有真实argv与失败信息测试。

这不要求现在凭空选择某个binding；要求G0不能只验证rename/ACL而遗漏包runtime的另一个外部语义。如果本阶段保持未证明，应把它明确列入G0未通过条件，不能后续实现者自由选择。

## R1-B3：非终端spawn环境缺少Windows必要项适配

位置：§5.5只写TerminalEnvironmentOwner；§8涵盖MCP/hooks但没有对应环境owner规则。

`conversation_kernel/mcp/sdk_facade.py:332-339`当前stdio仅继承HOME/LANG/LC_ALL/LOGNAME/TMPDIR/USER+固定PATH，缺SystemRoot等Windows必要环境。SDK default-env虽已有Windows baseline，当前Pulsara有界stdio不会走SDK public stdio_client，所以“mcp SDK支持Windows”不能自动修好。SystemRoot缺失可能让side-by-side assembly或子工具启动失败；PATHEXT/Path/PATH重复还会影响cmd解析。hook executor当前是另一套scrub后环境规则，不能为了统一平台把终端的完整继承策略强塞给它。

具体修法：§8明确MCP stdio使用经分类的Windows必要惰性环境baseline（如SYSTEMROOT/TEMP/USERPROFILE/PATHEXT等，实际集合由G0记录），保留声明lookup_path与secret_env精确resolver，以及spawn最后临界区的credential_guard。环境key按Windows规则唯一化，不能通过Path/PATH双份覆盖reviewed值。hooks保留其现有overlay/实际secret scrub规则并验证Windows key/COMSPEC/encoding语义。公共OS环境规范化小函数可以复用，业务继承策略不能混成“全量process.env”。G0-R或G0非终端process项加入真实MCP executable/cmd launch、中文路径、依赖SystemRoot的子程序、动态credential mutation与不漏key的输入/输出证据。

## 已认领的补充中还需带上的细节

- diagnostic named-pipe的“当前用户SID DACL”应明确**local-only**（如Windows PIPE_REJECT_REMOTE_CLIENTS能力），保留Unix私有socket的本机范围；per-home endpoint naming与多home隔离由现有home owner导出，勿造持久注册表。
- 已验证锁定pywin32 **b312** API，`win32file.i:5039-5071 SetFileInformationByHandle(FileRenameInfo)`的dict是`{ReplaceIfExists, RootDirectory, FileName}`，RootDirectory是目标目录handle。优先试这个真实extension point再声称必须自写rename primitive。
- §6.3初稿170行“根相对`\\x`”建议写成单反斜杠的`\x`；`\\server\share`另归UNC，避免规格示例自身混淆。

## 跨专题核对结论

- 支持冻结native Win11 25H2 x64、本地NTFS、G0前置且失败回规格的路线。WSL成本和语义明确，不应替换默认路线。
- 终端scope/Job/root-exit与managed-set分离、stdin不被命令payload占用、ConPTY EOF未证明则G0失败，均正确；对PS7退出码bootstrap可行性和1Mi字符通道仍要真实probe，规格没有假定已成功。
- Electron/CLI/installer矩阵与OS session-end边界、用户级页面独立性、provider-prefix连续性和三平台回归都已覆盖；不需扩大本轮scope。
- G0、实现与支持激活被清楚区分，估算也把上游长期fork排除，未将未知风险伪装成确定工作量。
- 本轮审阅未改根spec或生产代码。修订版需再通读并明确“可冻结”或保留阻断；本文件不是默认批准。
