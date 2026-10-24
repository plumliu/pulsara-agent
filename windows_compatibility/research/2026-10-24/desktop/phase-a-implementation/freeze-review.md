# Phase A实现与Mac证据：最终冻结审阅

2026-10-24，desktop / gpt-6.1-sol high。已通读根规格文首状态、§3.2和§16全部内容，以及root `final-review.md`、`macos-gui-review.md`、`gui-cleanup.log`，交叉核对terminal `evidence-review-final.md`和backend `final-review.md`及原始实验结果。未修改生产/根spec，未重建、运行GUI/provider或触及保存settings。

**明确同意冻结本次Phase A实现与实际Mac证据，无剩余阻断。Ubuntu native x64/arm64和真实Ubuntu GUI/安装资格仍按用户要求待验收；不批准进入B、不宣称A全平台验收完成、G0-W通过或Windows激活。**

## 实现与资格状态

根§3.2已把A当前实物与B计划明确分开：只读本机五字段locator、POSIX host和独立PG physical port、main/runtime/PG/CLI实际caller hard cut。未把guest资源、WSL helper、TLS/OAuth或Win32条款提前实现；未改Python业务/provider/schema/前端。§16准确记录唯一owner、原错误/路径/状态语义、PG raw/resolved修正与真实删除cwd回归，和源码最终形态相符。

§16分别记录改前31/23、最终Node46/46与Python23、真实资源relocation、checkout外wheel、startup/browser、独立PG/kernel生命周期、Mac app-bundle GUI，以及Ubuntu待验收。没有把早期45/45冒充最终结果，没有把Mac、容器或旧release当Ubuntu/Windows资格。主spec保留A/B/G0和激活的独立条件，用户允许当前Ubuntu待验收没有被写成矩阵删除或豁免通过。

## 证据交叉核对

- 已读root最终verify/Python/real lifecycle原始日志：Node46通过0skip，Python23通过；PG扩展/搬移Python/Chromium与资源版本检查成功；real lifecycle包含真实init/reuse/reset、ready/inspect/normal quit/锁释放/重开。不是只依赖审阅者结论。
- terminal独立EOF原始日志显示真实随包Python、preparePG原Error对象保留、未发start/未起PG、exit0/close、running=false、两次wait-owner、仅正常lock inode与自有home清理。
- backend实际PG JSON显示真实PG17.9与同PID/start-time；错socket或raw目录拼写拒绝未误停/删，版本16拒绝保sentinel/control，最后精确status3再移除自有home。它支持失败边界，不扩展为未知实例可接管。
- desktop自己的两包来源实验已证明18个ASAR模块与当前dist完整字节相等，私有Python逐文件复制到中文/空格/单引号路径后两组version/help/source probe均exit0。无hash/指纹registry，CLI实验没有registerCli。
- root Mac GUI记录的正常退出/重开、精确测试kernel暂停时保stopping与PG、版本99拒绝/重试不清库、恢复17后重开与正常退出，属于新app bundle/LaunchServices本机证据。已读slow-quit PID记录、独立PG日志完整shutdown序列和cleanup记录；没有把它扩大为DMG分发/Gatekeeper公证、UbuntuGUI、真实provider或WSL验证。

## 测试CLI副作用与清理已闭合

根§16.3与Mac GUI报告均如实记录：测试HOME未隔离macOS Electron os.homedir，best-effort CLI曾短暂指向测试包；root恢复production并临时冲突保护，最终移除保护。没有改前完整快照，不能断言未知旧内容逐字恢复。该限制没有被本专题独立CLI实验的“无注册”掩盖。

本轮额外只读复核并保存`cleanup-readonly-check.json`，实际观察为：

- `/Users/plumliu/.local/bin/pulsara`是可执行普通文件，不是symlink；其完整字节等于当前未变launcher算法为`/Applications/Pulsara.app/Contents/Resources/python/bin/python3`生成的production脚本，没有测试包目标或临时保护。
- GUI、terminal EOF、backend PG与root real lifecycle四个确切临时home均已不存在；未见测试installed bundle路径的进程。
- 原用户app PID20252和kernel PID20270仍分别运行`/Applications/Pulsara.app`及其随包Python。

此次只读确认没有注册/恢复CLI、停止进程或删除任何对象。root的恢复与清理事实足以关闭本轮测试隔离副作用；后续GUI harness仍须显式保护用户CLI，不应再假定HOME或PULSARA_HOME足够隔离。

## 冻结边界

同意root按本候选仅更新最终审阅/冻结元数据。冻结的是已实现的最小A与所列Mac实证，不是所有目标平台资格；Ubuntu待验收、B/G0-W未执行、Windows未激活的状态必须保留。后续若新增B实现、变更生命周期/认证/数据/发布语义，仍须相应授权、闸门与审阅。
