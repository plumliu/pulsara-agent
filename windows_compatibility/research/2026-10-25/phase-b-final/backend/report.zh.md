# alpha.10 正式 PG owner/helper：五个故障边界

2026-10-25。本批五个所选场景通过；未发现需要修改生产代码的缺陷。`verified-results.json` 对原调用顺序、分类、实际锁/PG事实和清理作离线断言。此结论补齐共享重验报告所列的五项 PG 整链缺证，不是任意失联组合、所有 Windows 环境或 B5 的资格声明。

## 实际执行层次

Windows Node **22.23.3** 从当前已装 alpha.10 `app.asar` 提取原 `postgres.js`、`postgres-operations.js`、`postgres-wsl.js`、`host-wsl.js`，逐一与当前 `desktop/dist` 完整 bytes 比较。实际使用原 BundledPostgres、typed errors、finite client 和固定 WSL launcher；没有复制 owner 或另造 startedHere。源基线沿共享重验 `0ea98e63` 加原 alpha10 七处版本同步/mise Windows 路径修正，本批四份 PG 生产源的 diff 为空。

Linux 使用 `/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3 -I`，实际 import 已装 `pulsara_agent.desktop_guest_ops`，完整源 bytes 与当前 ext4 clone 相等。PG 是同版本资源内的真实 PostgreSQL **17.9**（原 postgres.log 有版本与 Unix socket 事实），UID1000、Ubuntu/plumwsl。各 case 是唯一短 ext4 `.pbfpg-*` home、独立 socket/cluster、禁 TCP；启动 helper 原 ext4/身份/路径校验正常执行。没有触碰正式 `.pulsara` 数据库、共享发行版生命周期、interop/TUN、资源依赖或版本。

测试适配只在原 HostOperations.spawn 将此次 guest 入口换成 `bootstrap.py`，随后仍调用已装 helper.main：

- `_emit` 前暂停 ready 或真实 failed-result 的发送时点，不伪造 ready/result。
- 旁观原 subprocess.Popen，原参数/FD/返回值不变，记录实际子 PID/start ticks 和继承 FD inode。
- 限于本次 child 环境的 LD_PRELOAD 在真实 postgres/initdb exec 或 pg_ctl exit0 尾部 SIGSTOP；不暂停共享系统进程。信号使用 pidfd，并复核 UID/start ticks/专用 home 或配置 argv。
- 独立 `control.py` 观察真实 flock、pg_ctl status、SQL 系统标识与路径；这些是测试观察，不替代 TS owner 的决策。其观察用 status 不是生产 helper 内有限 status 的继承测试。生产 helper 内实际 status 的 kernel FD 在 `children-*.jsonl` 单列。

## 五项结果

| 场景 | 确定性门与实际事实 | 原 owner 结果 |
| --- | --- | --- |
| `pre-cancel` | helper 已持双锁但 ready 尚未发出；AbortSignal 令 stdin EOF，实际 operationBytes=0。恢复 helper 并等其退出0后，另一个真实 TS owner 显式启动同私有 PG；原 owner 的 catch 尚未运行。 | not-submitted、startedHere=false、无 status/readInstance 追认；此时真实 PG 已运行。随后测试明确调用原 stop/reset，不是自动补偿。 |
| `start-loss` | 真 pg_ctl start 已收到330 bytes+EOF；postgres exec 门暂停、尚无 pidfile。杀准确 helper 后 status3、kernel 锁空闲、PG lifetime 锁忙。独立竞争 start/reset/maintenance 三者均 PG_LIFETIME_BUSY/not-submitted，两个 sentinel 不变。释放 PG 门后同 systemid 实际 SQLready。 | 原 carrier 最终 exit9，原调用 unknown/startedHere=false；没有自动读回或重发。测试最后明确 stop/reset。 |
| `stop-loss` | 真 stop 已停止 PG/status3，pg_ctl 在 exit0 尾门未退出。杀 helper 后 PG lifetime 空闲，有限 pg_ctl 仍持 kernel FD。竞争三操作均 PG_KERNEL_BUSY/not-submitted；未删数据。释放旧 pg_ctl 后才两锁空闲。 | 原 stop unknown，既有 startedHere=true/initialized=true 保持；最后明确 reset 清除，不因 PG 已停就提前允许新 start 与旧 stop 重叠。 |
| `init-loss` | 真 initdb exec 门继承 kernel+PG 两个 FD；杀 helper 后两锁仍忙。竞争三操作均 PG_KERNEL_BUSY/not-submitted。释放 initdb 后其准确 PID 退出、两锁空闲；实际未留下 PGDATA。 | unknown、initialized=false、startedHere=false，无自动 initdb 重放；原只读 maintenance 对空布局成功，再精确清 home。未假定 unknown 表示未执行。 |
| `readiness` | 支持的 PGCTLTIMEOUT=1，加 postgres exec 门，让真实 pg_ctl 结算 exit1。原 helper 已形成 failed 结果，仅发送前暂停。独立恢复 PG并 SQL核同 systemid 就绪，再放行原失败帧。 | 原 carrier exit0/完整结果：failed/code1，原 stdout/stderr `server did not start in time` 保留；原 start catch 实际 status+readInstance 后 startedHere=true，同时仍抛原错误。 |

`verify_results.py` 对全部调用序列精确比较，明确扣除测试主动的竞争检查、stop、reset 和 maintenance；没有把“全部后续调用未 armed”当作无重放证明。readiness 保留原 status/readInstance，unknown/未提交没有多出的自动读回。所有 carrier 均有真实 close；实际 mutator 的在途事实由锁、子身份和 PG/SQL独立观察证明，未以 carrier close 代替。

真实 reset 的删除均由原 owner→helper 在 fresh status3/双锁下完成，外置锁 inode 在前后事实中保持、outside sentinel 保持。initdb 结算后的无目录分支不调用 resetConfirmed 冒充删除；原 maintenance 对该空布局准入后才清理整个本次实验 home。

## 两次原 fixture 失败

1. `first-pre-cancel/result.json`：同步测试观察阻塞 Node event loop，目标 start 尚未到达，停在前置 prepareLayout；已正常停的原私有 PG 没有在途启动。保留 `owner.blocking-snapshot.cjs`、原 failure/rescue/safe-checkpoint。救援只对已核准确的该布局 helper/观察器发信号，随后通过原 TS resetConfirmed 清已停止 PGDATA，两锁空闲后删精确 home。旧 JSON 的 `known_readonly_setup_only` 用语过窄：prepareLayout 一般允许建立 socket 目录；这里指已知前置布局路径，不能据此将该生产方法泛称纯读取。修 fixture 为先 await 原 client 到达目标 spawn/实际 stdin finish，再同步观察。
2. `first-init-loss/result.json`：核心 unknown/继承锁/拒入已完成，但 fixture 假定 initdb 必留下 PGDATA，调用 resetConfirmed 得 ENOENT。原失败保留。实际未留下目录，不推测具体退出原因；修 fixture 为仅存在数据才走原 reset，否则走原 maintenance，重跑该单项通过。没有改生产 empty-layout、异常分类或 reset 断言。

当前脚本含上述 fixture 修正；不是每个历史失败的逐字旧脚本。blocking snapshot 保留首种阻塞逻辑（后来追加的 control-cleanup 工具入口不影响它）；第二种旧收尾是假定无条件 `await owner.resetConfirmed()`，此差异在本文明确说明。历史结果不改写为产品失败或成功。

## 命令与清理

五场景分别执行：

```powershell
& 'C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe' output/phase-b-final/backend/owner.cjs pre-cancel
# 同命令末参依次为 start-loss / stop-loss / init-loss / readiness
```

每次记录真实 config/home/library，`tool-homes.jsonl` 为本批工具目录列表，仅用于本次精确清理，不是生产 registry。helper/子/gate/PG log 小原记录在各 case `guest-records/`。

`cleanup.py` 通过已装 Python -I 执行：六个现存私有 home 均先验证数据不存在、两锁非阻塞可取、outside 不变；所有已记录 PID/start ticks 均无同身份存活，accessible 同UID argv/environment 没有本次 home。随后仅删除这些 canonical、UID1000、0700、严格 parent/prefix 已核目录及六个 `.pbf-tools-*`。另一个首失败 home/tools 已于人工睡眠前通过 `safe_checkpoint.py` 删除；总计七组实验目录，无留下 PG/helper/finite mutator 或 gate。当前同 UID `/proc` 369/371 不可读单列，未声称全权限全机无残留。Windows carrier 的 close 则由原 Node 调用记录逐一确认。

随后运行项目 `.venv/Scripts/python.exe .../verify_results.py`：**5 passed**，两次原 fixture failures retained。没有新生产修改，因此未为这批测试重建资源或重复整个 Node/pytest suite。

实际 saved credentials 精确 contains 扫描：共享重验 root29文本、现场睡眠 root43文本分别0命中；从 LocalSettingsStore/require_pulsara_home 只读加载，不输出 secret，不连接 provider。后续本目录归档同样扫描，结果另列。报告保留具体路径、PG身份、错误和输出，没有宽泛遮盖证据。

## 资格边界

本批证明的是五项所选真实 PG 边界；完整帧/分段 UTF8/畸形/重复/partial/4MiB 等仍引用原生产 client/helper focused tests，不混成这里真实 PG 的所有协议组合。没有额外声称 Windows carrier/Node 自身死亡的每种 mutator 交错、真实满盘、未安装 WSL、原生 Ubuntu/Mac、双屏或其他物理输入法已验。无provider调用、无任务恢复/replay/第二归属标志。R1 的27项 ROOT/prefix确定性测试和 R5 的真实 provider 输入连续性证据仍是独立来源，不把本批五项相加当作模型 prefix 证明。
