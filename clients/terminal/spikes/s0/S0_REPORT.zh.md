# Bubble Tea v2 S0 可行性证据报告

> 日期：2026-08-01
> 结论：`PASS`；production S1–S6 可以开始
> 延后项：真实IME、attached tmux视觉检查与非本机clean-runner启动保留为后续兼容性/release regression

## 冻结版本

- Go `1.26.5`，module language baseline `go 1.25.0`
- Bubble Tea `v2.0.6`
- Bubbles `v2.1.0`
- Lip Gloss `v2.0.5`
- Protobuf Go runtime `v1.36.11`
- tmux `3.6a`
- OpenSSH client `10.0p2`

Bubble Tea `v2.0.6`的官方release明确包含wide-character handling修复；spike使用正式`charm.land/bubbletea/v2` import path，没有回退到旧GitHub v2路径。该验证对应的renderer-critical Ultraviolet版本为`v0.0.0-20260416155717-489999b90468`，production module必须保持同一compatibility pin，除非完整重跑Apple Terminal resize、wide-rune和render-jitter gate。

## 自动化结果

### Model、textarea与paste

- 已组合UTF-8 `中文🙂ASCII`、中文标点和wide rune插入/删除通过。
- Home/End、前后移动、multiline与dynamic height通过。
- Shift+Enter typed message与Ctrl+J fallback通过。
- Bubbles textarea没有native undo stack；spike验证了bounded client-owned undo/redo seam。
- 1 MiB bracketed paste不进入textarea，转为`bytes + SHA-256`状态；当前两档真实PTY smoke耗时分别约195ms和132ms。

### 并发fake Protobuf stream

| rate | events | draft | keypress p95 | stream delivery p95 |
|---:|---:|---|---:|---:|
| 20Hz | 60 | 完整 | 17.942ms | 1196µs |
| 100Hz | 300 | 完整 | 17.328ms | 219µs |

Snapshot/delta通过独立继承FD上的length-prefixed fake Protobuf传输；Go child的stdin/stdout只属于PTY，未连接production protocol。

### 可重复性能基线

性能fixture固定为每档20次重复、1秒warm-up、3秒active window、10Hz process sampling和每轮20个交错CJK keypress probe。每条轨迹都验证exact delta count与完整draft；percentile使用nearest-rank，workload表中的数值是per-run统计量的跨run p95，RSS peak取全部run最大值。

| workload | runs | keypress p95 | delivery p95 | renderer write p99 | render jitter | CPU average | RSS peak |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 20Hz | 20/20 | 20.227ms | 0.429ms | 87.701ms | 16.759ms | 4.500% | 15.578MiB |
| 100Hz | 20/20 | 20.418ms | 0.762ms | 45.035ms | 1.866ms | 10.039% | 16.828MiB |

Renderer cadence来自Bubble Tea最终PTY output writer的non-empty physical writes；它不使用Python PTY read分包，也不声称等于terminal display hardware refresh。20Hz下输入probe与50ms delta cadence交错，因此`p95 - p50` jitter高于100Hz；两档均通过冻结gate。CPU通过macOS `proc_pid_rusage(RUSAGE_INFO_V2)`采样，不依赖`psutil`。

冻结的feasibility gate为：keypress p95/p99不超过50/100ms；stream delivery与producer schedule lag p95不超过10ms；renderer interval p99不超过100ms、jitter不超过50ms；20Hz/100Hz CPU average跨run p95不超过25%/50%，CPU interval peak不超过150%；RSS peak不超过128MiB，每轮首尾quartile median growth绝对值不超过16MiB。跨run`p95 - p05`允许方差分别为keypress p95不超过25ms、CPU average不超过20 percentage points、RSS steady p95不超过16MiB、renderer interval p95不超过25ms。40/40轨迹的correctness invariant与全部统计gate通过。完整阈值、每轮summary与host/binary identity见`evidence/darwin-arm64-performance.json`。

### Crash与signal

- normal quit、intentional panic、SIGTERM、SIGINT：Bubble Tea退出后恢复canonical/echo/signal terminal flags。
- SIGKILL：child无法自行恢复，Python parent检测退出并执行emergency termios restore。
- 五种轨迹中，Python parent的独立probe operation均持续推进；UI退出没有取消parent operation。

### tmux与SSH

- tmux 3.6a：alternate screen和bracketed-paste mode均完成enter/restore；连续resize与1 MiB paste通过。
- Docker内真实OpenSSH remote PTY：`TERM=xterm-256color`、`LC_CTYPE=UTF-8`、60ms server-egress netem、CJK输入、强制断线和reconnect通过。
- 真实远程host `plumliuwin.local`：macOS OpenSSH经Windows OpenSSH/ConPTY进入WSL2 Linux x86_64；remote artifact SHA-256一致、UTF-8、`TERM=xterm-256color`、CJK、alternate-screen与bracketed-paste通过。20次keypress p95/p99为98.972/100.636ms，首次frame约403.669ms；分别低于远程专用150/250ms与3s gate。
- 正常退出由SSH恢复PTY；本地SSH被SIGKILL时无法自行恢复，Python parent按契约执行emergency restore。远端Bubble Tea process随后退出，reconnect version通过；WSL `/tmp`与Windows staging file均经删除后absence probe确认。
- 这已关闭真实host/network/PTTY/disconnect/reconnect项；真实macOS terminal emulator中的IME pre-edit/candidate视觉记录保留为非阻塞兼容性证据。

### Packaging

- 生成darwin/linux × amd64/arm64四个`CGO_ENABLED=0` binary。
- 四个binary均通过file-format、embedded Go module version和SHA-256检查。
- 本机darwin/arm64完成`--version`及`--self-test`启动。
- darwin/amd64、linux/amd64、linux/arm64尚未在各自clean runner完成启动；该项进入S6 release gate，不再阻塞S1。

## Deferred兼容性与release证据

1. macOS真实中文IME pre-edit、候选切换/提交与中文标点。
2. family emoji/grapheme在真实terminal中的逐位置光标视觉检查。
3. 支持keyboard enhancement的terminal上的真实Shift+Enter；不支持时的明确fallback观察。
4. attached tmux client中的IME、mouse和paste记录。
5. 四个平台clean runner启动记录。

以上项目不再是S1 admission blocker。S0冻结结论为`PASS`：Bubble Tea v2.0.6路线没有发现framework/process可行性阻断项；production实现仍须在S1–S6逐阶段通过各自gate。复现命令与人工步骤见同目录`README.md`。
