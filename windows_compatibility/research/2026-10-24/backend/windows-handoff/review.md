# Windows准备/G0-W新手清单全文审阅

2026-10-24；backend；gpt-6.1-sol/high。

**结论：同意当前401行 `PULSARA_WINDOWS_DEVELOPMENT_G0_CHECKLIST.zh.md` 交付，无剩余阻断。** 已通读全文，交叉核对terminal `recommendations.md`、desktop `proposal.md`、本专题提案及现冻结spec/实际脚本。以下小幅精确化建议不改变路线，不要求B实现；root如采纳仅需复核对应段落。

## 已闭合的执行边界

- 清单明确只是Windows接手说明，未把本机A或文档检查称作Windows/WSL已实测；A Ubuntu资格仍待验收，B/生产WSL adapter/安装器/picker/TLS/OAuth桥不进入本轮。
- 两份同Git提交、各自依赖/缓存的clone与执行终端区分清楚；Windows只装host Node/Electron/tsc和适用小集合，不误跑POSIXmise/package/main或全Node fixture。Linuxrepo在ext4，拒共用`.venv/node_modules/resources`及C盘映射构建。
- WSL引擎系统前提与普通用户运行分开，固定Pulsara/nonroot；不接管同名未知实例、不改默认发行版/全局config、不用unregister/shutdown重试。只有新实验环境初期配置生效才明确terminate该目标，未被包装成日常Quit。
- 已再次浏览Canonical指定rootfs目录，文件名、SHA256SUMS及签名确实存在。current明确浮动，首次实验保存实际制品/日期/build/包版本，未把URL或本文当已冻结WSL资格；不以Mac制品代替Linux。官方制品摘要验证是AGENTS认可的真实内容边界，没有代码/文档证据hash。
- 用户创建、默认/显式身份、interop/automount/PATH分别检查；PE正/负对照明确排除“/mnt/c路径不存在”假通过。禁interop不伪装对恶意Windows用户/root的沙箱。
- Linux依赖来自现workflow/build_environment；pin与现repo一致，root仅OS维护、nonroot做uv/npm/build/initdb。Playwright OS依赖经项目实际venv/root维护入口，后续resources仍普通用户。没有无依据要求系统PG、Rust/WindowsPG/pwsh或VisualStudio。
- 当前`prepare_resources.target_platform()`与所有components的Mac/Ubuntu限制被忠实保留；Linux `desktop:verify`的资源/PG/Chromium实际副作用及证据范围明确，不当作跨WSLhelper、WindowsGUI、TLS或nativeUbuntu资格。
- 7.1临时rawpipe probe有真实cwd/用户/三pipe/EOF/stderr/exit7，输入规模没有冒充产品cap。其代码/命令显式待Windows运行，结果不自动当JSONL/取消/后代join全部通过。
- P门含真实资源、Unixsocket/role/schema、TS owner状态/error与错实例/版本保数据、有限helper锁/EOF/未知结果；一次status3不能替整门。F门包含唯一Linux-backed用户级GUI、实际mount/identity与File字节来源、preview/HTML/下载安全；mkdir/Explorer不代产品picker。N门区分空目录HTTP可达与连接级TLS身份、旧Session/rebind/Origin/browser隔离、OAuth SDK所有权，没有HTTPchallenge/0.0.0.0/忽略证书fallback。
- 保存设置/凭据、未知PG结果、精确清理、output被忽略不随Git带走、后续stage/提交与源码同步边界均清楚；新手不会误以为Mac详细输出已在clone中或被授权运行provider/进入B。

## 非阻断精确化建议

1. **wsl.conf编辑操作**：§4.4基本APT列表不保证有文本编辑器，建议加例如nano的明确安装/保存退出说明，或在新无工作发行版用固定here-doc，避免新手停在“编辑文件”而不知道用什么。不得盲追加重复section或覆盖未知配置。
2. **镜像签名key差异**：§4.2链接的通用Ubuntu ISO教程可复用验证流程，其示例CD-image key ID不能自动当WSL/cloud image签名key。建议注明从该制品官方可信来源核完整fingerprint，再verify；不能照抄教程示例key。Ubuntu安全FAQ单列Cloud Images/checksums与simplestreams签名key，支持应按制品辨认，而非“遇到哪个key就信哪个”。[Ubuntu Security FAQ](https://wiki.ubuntu.com/Security/FAQ)、[当前WSL目录](https://cloud-images.ubuntu.com/wsl/releases/24.04/current/)
3. **WindowsNode来源**：在§3.3补 `node -p`打印platform/arch/version/execPath便于新手识别错误终端，预期win32/x64；版本号本身不证明平台。当前文中已有明确预期与禁止伪装，所以不是阻断。

Canonical当前WSL目录实际显示2024-04-25 rootfs时间；清单没有称其最新，APT/包版本与后续维护资格须按实际记录，不应以目录名current掩盖旧基镜像。该G0初始材料选择不等于生产rootfs recipe已定。

本审阅只读/浏览官方资料并新增个人报告，未运行Windows/WSL/DB/provider、未改production/root/测试、未构建或执行Git mutation。用户授权的stage/commit/push由root统一处理；本批准只覆盖新清单交付，不构成Windows通过/B授权。
