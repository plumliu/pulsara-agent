# Phase A最终实施与新包来源交叉审阅

日期：2026-10-24。范围：本轮授权仅冻结WSL规格的Phase A，新增实际并行实验仅检查候选包来源及独立CLI。未编辑生产/项目测试/package metadata，未运行tsc/npm/mise、GUI、provider或保存settings操作。

**结论：当前Phase A源码、已核对的新Mac候选包来源与本专题CLI实证无剩余代码阻断；这不等于A所有平台资格完成。Ubuntu native x64/arm64及Ubuntu GUI证据仍必须按根规格补齐。用户CLI恢复由root完成并确认，不能根据本实验宣称全轮从未触及用户CLI。**

## 1. ASAR完整字节检查

使用已安装维护SDK `@electron/asar 3.4.1` 的`extractFile`，对两份候选产物分别读取：

- `output/windows-compat-20261024/root/phase-a-implementation/package/mac-arm64/pulsara.app/Contents/Resources/app.asar`
- GUI测试clone `output/windows-compat-20261024/root/phase-a-implementation/installed/pulsara.app/Contents/Resources/app.asar`

每个ASAR内的`dist/{main,runtime,postgres,resource-paths,host-operations,host-posix,postgres-operations,postgres-posix,cli-install}.js`与当前`desktop/dist`对应文件执行完整`Buffer.equals`。**两包共18/18完全相等**，同时记录实际字节数；没有生成文件fingerprint/hash、额外版本manifest或证据registry。

因此两份被测app携带的是最终dist：包括PG `readInstance('raw'|'resolved')`边界修复、四参runtime、同步host resolver、纯资源locator与CLI已解析Python接入。不是仅凭版本号或目录存在判断“新包”。

## 2. 真实随包Python和CLI

对两包分别在本专题工作区创建独立临时目录，将该包完整私有Python目录用macOS APFS逐文件clone复制到`中文 资源's path/python`。目标根为真实复制目录，**不是指向原资源的symlink alias**；正常`bin/python3`自身相对symlink随包保留。另对`pulsara_agent/{__init__,__main__,cli}.py`与来源包逐完整字节相等检查。

通过当前dist的`desktopResourcePaths`与`cliLauncherScript`在临时目录生成launcher，但从不调用`registerCli`。不启动app/GUI，不运行`pulsara app`。窄child环境仅含固定系统PATH/locale、禁pyc写与本次不存在的隔离`PULSARA_HOME`；无PYTHONPATH/PYTHONHOME或保存credential环境，cwd为独立scratch。

每包实际完成：

1. 随包Python `-I -B -c`来源probe：`sys.executable`精确为含中文/空格/单引号的本次Python路径，`sys.prefix`精确为复制根，`pulsara_agent.__file__`位于该根自己的site-packages，版本`0.1.0a6`。
2. 生成的真实CLI `--version`：exit0，stdout `0.1.0a6`。
3. 同CLI `--help`：exit0，完整usage/管理命令/--version及app说明存在。

**两份bundle合计6个实际命令均exit0、无signal**；隔离Pulsara home未被创建，scratch成功后全部删除。来源/命令/退出/完整stdout/stderr和launcher原文在`package-cli-evidence.json`；独立help/version stdout同时保存。本检查没有读取或写用户真实CLI路径。

首次实验harness将argparse自动换行的帮助短语误当单行，只有该格式断言失败；原文保留在`package-cli-evidence-first-run.json`。修为仅归一空白、仍匹配完整同一说明文字后重跑两包成功；未改变CLI生产代码或项目测试断言，未丢弃第一次失败记录。

## 3. 与root统一验证交叉核对

已只读核对root `verify-final.log`：最终tsc/npm test **46/46通过、0失败、0skip**；随后真实资源relocation包含PG/vector/pg_search、Python与Chromium，并显示原release resources check版本0.1.0-alpha.6。未把此前PG修复前45/45沿用为最终结果。

源码边界仍为最小A：main保持原boot/reset/quit顺序；locator纯五值；runtime/PG实际caller显式注入；CLI脚本marker/quote/argv/退出和PATH行为不变；PG raw/resolved观察按实际consumer执行。没有实现WSL/TLS/OAuth/WindowsGUI/部署、原生Win32内核或Python全库平台重构。

## 4. GUI隔离限制与尚缺资格

root明确报告：GUI测试clone的LSEnvironment HOME并未隔离macOS `os.homedir()`；打包app原有automatic CLI注册曾短暂更新实际用户CLI。root已恢复production指向，并使用临时symlink冲突保护；**最终恢复常规launcher文件及状态由root负责确认**。这说明只隔离PULSARA_HOME/LS环境不足以声称没有用户级CLI副作用；不能把本专题这次独立CLI实验无注册，推广为整个GUI测试从未碰CLI。

本实验没有启动GUI或改上述保护/launcher，也没有碰正在运行的`/Applications/Pulsara.app`。其证据只覆盖包来源、真实私有Python搬移路径、CLI引用/usage/退出，不能代替已安装GUI启动/慢退/重开、native Ubuntu、Ubuntu GUI、WSL G0或真实provider。

按冻结规格，缺native Linux/GUI证据时最终状态应为“Phase A实现完成、相应验证待补”，不能宣称A全部资格通过或Windows支持激活。root可汇总其已完成Mac GUI/容器补充证据，但必须分别标明实际边界。

## 证据文件

本目录：`verify-package-cli.cjs`、`package-cli-evidence.json`、`package-cli-evidence-first-run.json`、`package-cli-version.stdout.txt`、`package-cli-help.stdout.txt`、`installed-clone-cli-version.stdout.txt`、`installed-clone-cli-help.stdout.txt`。此前源码与跨专题审阅见`implementation-notes.md`、`cross-review.md`、`preflight.md`。
