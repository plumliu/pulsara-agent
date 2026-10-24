# 第六轮 G0-D：确认目标、离线私有资源与失败保数据

受测基线 `b741dd92b06a2d167d882e312e18f503e23b8a89`，沿已确认的Ubuntu/plumwsl，未改生产/spec/依赖、系统默认、WSL配置、VPN或用户settings；未提交或进入B。已读当前spec §2/3/7.2/8与G0清单7.2，并检查实际prepare_resources.py、resource-paths.ts、package.json资源映射和smoke入口。

**本轮闭合所列目标发现/固定调用及私有payload有限安装的机制子项：真实无网络、非root namespace中安装完整当前资源，缺制品/坏摘要/写点ENOSPC/中断部分stage均拒绝发布，数据sentinel保留。它不是产品setup owner、持久选择UI或正式installer；实际默认变更、WSL未装/需重启等不在本机伪造。完整G0与Windows激活未由本报告授予。**

## 实际WSL发现与固定目标

`discovery_probe.py` / `discovery-results.json`：使用系统实际SystemRoot/System32/wsl.exe，直接argv、无PowerShell求值，清本次WSLENV传递。保存原WSL version/status/list、三pipe输出和必要base64原字节，不改全局设置。

- 实际默认无-d/-u调用发现Ubuntu/plumwsl、UID1000、passwd真实home `/home/plumwsl`；发行版ID=ubuntu，home findmnt为ext4，list实际Ubuntu行是WSL2。Windows IsUserAnAdmin=false；没有UAC/sudo或以root运行资源工具。
- 用户在此前任务明确确认该目标，本轮将完整typed值仅记为研究confirmed-target.json；后续全用显式-d Ubuntu/-u plumwsl/--cd真实home重开，并重新核实际身份/OS/home。这个JSON不是产品host settings或新registry。
- 缺失发行版和未知用户使用真实唯一不存在名称调用WSL，均返回4294967295及原错误，不接其他目标。
- root通过当前非root用户真实getent得到UID0；研究目标准入在launch前拒绝ROOT_TARGET_REJECTED，没有运行root WSL子程序。
- “默认已改为另目标/root”只构造发现输入阴性，确认值保持不变，实际重开仍显式使用Ubuntu/plumwsl。**没有修改真实系统默认，也没有生产setup持久选择逻辑可验收**；这里只证明研究固定参数路径不会重新从该输入选目标。
- 前后实际默认身份相同，interop/appendWindowsPath/automount/mountFsTab只读观察，Windows PATH存在、/mnt/c仍挂载，实际cmd.exe /c ver退出0。没有重启、终止或注销共享发行版。

## 现有owner与制品边界

当前prepare_resources没有shipping WSL installer或既有组合tar格式，不能宣称找到了产品安装API。研究只把现package.json的resources目录及bin/pulsara映射装入一个本地试验tar，不加入新的生命周期状态机：

1. tarfile标准库打包当前完整Linux resources，加入原pulsara-cli.sh；约1.595GB，真实artifact size=1595002880。这个tar仅为有限stdin传输容器，不是本轮批准的产品格式。
2. Windows宿主连接两个明确WSL子进程：Linux cat stdout→Windows Python byte读写→Linux receive stdin。producer/receiver均exit0，字节数完全匹配；没有把payload放到NTFS，也没用PowerShell文本管道传它。
3. 接收完整tar后，以同一实际SHA256确认不可变制品字节再rename缓存；随后直接复用原source_tree的缓存摘要校验与tarfile filter=data解包，以及原validate_resources/pg_layout/install_extension/validate_architecture。SHA只在真实制品边界使用，不是代码/文档hash或fingerprint registry。
4. 校验通过后才在同ext4内将stage/payload单rename到自有版本资源目录，root0700；数据根/工程根与资源根分离。没有改系统Python、PG、Node或用户工具。

没有实现installer调度/自动恢复/journal、兼容双写、第二settings、服务或队列。所有动作一调用一退出。现recipe对已存在的source folder不会重新解包，因此**部分stage重开仍必须过原完整validator**；它不会凭目录存在就获得运行资格。

## 真实离线与有限失败矩阵

`payload-results.json`包含原始命令/输出、精确进程门和sentinel事实。Linux每次安装使用 `unshare --user --map-current-user --net`：UID实际仍1000，新的network namespace连接保留测试地址得到原生ENETUNREACH101；recipe下载入口调用列表为空。没有改主机或共享发行版网络配置，namespace随有限进程退出。

| 场景 | 真实观察 | 发布/数据 |
| --- | --- | --- |
| 已提供本地完整制品、离线 | 原recipe全native架构、PG17.9/vector0.8.1/pg_search0.26.1/Python3.12.15校验；隔离-I导入wheel/私有rg路径成功 | 发布0700版本目录，三个sentinel相同 |
| 缺制品 | 实际FileNotFoundError/ENOENT2；不将缺本地文件解释成下载授权 | 未发布，sentinel相同 |
| 坏制品摘要 | 原recipe真实cached upstream checksum mismatch | 未解包/未发布，sentinel相同 |
| 解包写点ENOSPC | 仅把实际tarfile.bltn_open目标写点重定向到/dev/full，真实内核OSError errno28 | 明确注入边界，不冒称整块ext4真满；未发布，sentinel相同 |
| 实际解包期间中断 | 原tarfile copyfileobj返回后发门、自SIGSTOP；宿主按pidfd+starttime对这个helper发SIGKILL，carrier非零 | 部分stage保留、未发布；不重发不明操作 |
| 新有限调用重开同一部分stage | source_tree看到目录后不重新解包，原validator真实因缺pg_config拒绝 | 未发布，sentinel相同 |
| 明确清本次stage后新有限安装 | 确认旧helper死亡后，仅删除已核自有interrupt stage，再另起新安装调用；仍在无网络namespace | 原recipe全校验后发布，sentinel相同 |

中断门位于真实tarfile文件复制回调，不是替身installer。copyfileobj已返回但文件仍处在SDK上下文中，**未证明该小成员已fsync或持久化**；本例证明真实创建中的部分stage不能成为完整资源。删除stage是实验显式步骤，不是产品自动repair/replay。

Sentinel位于隔离root内的home/.pulsara/local-settings.yaml、home/.pulsara/desktop/postgres/keep、home/workspaces/keep；它们是事先建立的模拟既有用户数据，不是真实saved settings/DB。每步精确比较相同内容。真实用户home/settings/workspaces不读写，此实验不能替代正式更新/卸载UI或运行中owner排他维护合同。

## 发布后新位置确实可执行

首矩阵在stage中校验后rename，未以它冒称已测发布后的路径。因此另做一个有限post_publication_check.py：复制当前完整资源、原recipe校验、rename后确认旧stage不存在，再在同样真实无网络/UID1000 namespace执行：

- 发布后Python -I导入pulsara_agent，module/sys.prefix/私有rg均位于新runtime目录，PEP440版本0.1.0a6；不是editable clone导入。
- 现pulsara-cli.sh真正 --version=0.1.0a6。
- 新位置postgres --version=17.9，pg_config实际lib/share都指向发布后的新layout。
- 私有ripgrep实际 --version=15.2.0。

这是独立发布后定位正控，不运行GUI、provider、PG服务或新数据库；没有替代完整app cold start和浏览器/PG功能资格。

## 验证、清理与复现

verify_results.py只读重新断言完整host传输字节/摘要、实际无网络、每个失败未发布和sentinel相同、中断重开拒绝、显式清理后的有限重装、发布后执行与根删除。verified-results.json passed=true。

最终verify_cleanup.py只读核12个记录的Linux PID+starttime、2个准确私有root，remainingProcesses/remainingRoots均空。Windows producer/consumer和有限WSL carrier均由创建它们的Popen对象等待回收；唯一强退是本次被确定性暂停的helper pidfd。未泛杀、未结束共享distro。1.595GB tar/cache/两个完整资源副本均未归档，实验根已删；只留小脚本和结构化证据。

Windows项目uv Python3.12.13负责编排，资源构建与验证在实际Linux clone/项目Python3.12.15上执行。读取现配方与维护SDK，没有安装新依赖。重现先运行discovery_probe.py，再payload_driver.py；后者生成新uuid私有root，在本机已确认的Ubuntu/plumwsl执行。post_publication_check.py须在上述非root私有network namespace运行。verify_results.py/verify_cleanup.py是只读后验入口。

本机缺少可用于真实“WSL未装/不可用/需重启”的安全日常环境，不能通过卸载/终止用户WSL制造；默认变更仅输入阴性。没有正式目标选择UI、持久host settings、新应用installer、版本更新/卸载与运行中owner互斥产品合同。这些未实现/未测项仍应在G0/B总表明确，不把本轮维护机制正证升级为整个Windows支持完成。
