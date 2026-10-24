# 本轮命令与日志索引

执行目录：Windows `C:\python_workspace\pulsara-agent`。全部目标均显式Ubuntu/plumwsl，无全局shutdown/terminate/unregister。下面是实际执行的探针入口；每份JSON日志的`command`/`invocation`项记录完整wsl.exe argv和Python代码，因此不依赖PowerShell转义后的回忆。

```powershell
# 固定Node入口；这些变量只为阅读缩短命令，没有保存凭据。
$probeNode = 'C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe'
$probeDir = 'output/windows-g0/terminal/20261024-interop'

& $probeNode "$probeDir/host-pipes.cjs" > "$probeDir/host-pipes.node22-python314.log" 2>&1
& $probeNode "$probeDir/host-pipes.cjs" /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python > "$probeDir/host-pipes.node22-python312.log" 2>&1

# 本次临时PE副本，cleanup已移除。复现前只在此独占output目标不存在时复制。
Copy-Item -LiteralPath C:/Users/plumlocal/AppData/Local/mise/installs/node/22.23.3/node.exe -Destination "$probeDir/node-pin.exe"

& $probeNode "$probeDir/interop-lifecycle.cjs" > "$probeDir/interop-lifecycle.node22-python314.log" 2>&1
& $probeNode "$probeDir/interop-lifecycle.cjs" /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python python312 > "$probeDir/interop-lifecycle.node22-python312.log" 2>&1
& $probeNode "$probeDir/interop-lifecycle.cjs" /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python python312-native native > "$probeDir/interop-native.node22-python312.log" 2>&1
& $probeNode "$probeDir/interop-lifecycle.cjs" /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python python312-native-repeat native cancel > "$probeDir/interop-native-repeat.node22-python312.log" 2>&1
& $probeNode "$probeDir/interop-lifecycle.cjs" /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python python312-native-root-repeat native rootexit > "$probeDir/interop-native-root-repeat.node22-python312.log" 2>&1

& $probeNode "$probeDir/runtime-eof.cjs" > "$probeDir/runtime-eof.node22-python312-source.log" 2>&1
& $probeNode "$probeDir/runtime-eof.cjs" /home/plumwsl/workspaces/pulsara_agent/desktop/resources/python/bin/python3 bundled > "$probeDir/runtime-eof.node22-python312-bundled.log" 2>&1
& $probeNode "$probeDir/node-crash.cjs" > "$probeDir/node-crash.node22-python312.log" 2>&1
& $probeNode "$probeDir/finalize-evidence.cjs" > "$probeDir/cleanup.log" 2>&1
& $probeNode "$probeDir/finalize-evidence.cjs" > "$probeDir/cleanup-final.log" 2>&1
& $probeNode "$probeDir/environment.cjs" > "$probeDir/environment.log" 2>&1
```

最终列出的所有入口实测退出0（初次失败另存`initial-*`/`node-crash-initial-*`）。命令执行后立即记录了PowerShell `$LASTEXITCODE`，原tool transcript中为EXIT=0。探针的断言验证字节和观察事实；生命周期probe code0不等于产品G0通过。

静态命令：读取根AGENTS、当前README/spec/清单、terminal历史与 `desktop/src/{runtime,host-operations,host-posix}.ts`；`node --version`、`wsl -l -v`、`wsl --version`、`wsl -d Ubuntu --exec sh -c 'id; python3 --version; uname -r; ...'`、只读pin路径ls/Get-Item与末尾`git status --short`。完整有效环境由`environment.json`重取，未dump环境或settings。

runtime-eof实际只创建已核不存在的两个Linux独占短home：`/home/plumwsl/g0-homes/terminal-eof-20261024-interop`和`...-bundled`，0700。未发start帧；原wait-owner后以flock再次确认无owner，只在完整路径/canonical/无symlink/目录内容均吻合时删除自己目录。

本轮工具源码随实验学习做过小幅修正（例如PE stdin隔离、增加普通.NET父、每次Nodecrash唯一record）。日志携带当时完整Python/launcherargv，报告保留每轮原因；并未以最终probe源码冒充所有历史运行都用相同字节。
