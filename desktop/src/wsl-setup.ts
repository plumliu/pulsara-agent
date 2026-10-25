import { execFile } from 'node:child_process';
import { createReadStream } from 'node:fs';
import { mkdir, readFile, rename, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import { promisify } from 'node:util';
import { executeWsl, guestAbsolutePath, hostPathInWsl, validateWslSelection, windowsWslExecutable, wslArguments, wslHostEnvironment, type WslSelection, type WslTarget } from './host-wsl';
import type { DesktopResourcePaths } from './resource-paths';
export type { WslSelection, WslTarget } from './host-wsl';

const execute = promisify(execFile);
export interface WslDistribution { name: string; version: 1 | 2; default: boolean; }
export interface WslPayload { archive: string; sha256: string; version: string; interopHelper: string; }
export interface WslPrepared { target: WslTarget; home: string; resources: DesktopResourcePaths; hostHelperPath: string; }

const decodeList = (bytes: Buffer) => (bytes.includes(0) ? bytes.toString('utf16le') : new TextDecoder('utf-8', {fatal:true}).decode(bytes)).replace(/^\ufeff/, '');
export function parseWslDistributions(bytes: Buffer, quiet: Buffer): WslDistribution[] {
  const text = decodeList(bytes);
  const names = decodeList(quiet).split(/\r?\n/).filter(Boolean).sort((a,b) => b.length-a.length);
  for (const name of names) validateWslSelection({distro:name});
  const result: WslDistribution[] = [];
  for (const line of text.split(/\r?\n/)) {
    const prefix = /^\s*(\*)?\s*/.exec(line)!;
    const body = line.slice(prefix[0].length);
    const name = names.find(value => body.startsWith(value) && /^\s/.test(body.slice(value.length)));
    const match = name === undefined ? undefined : /\s([12])\s*$/.exec(body.slice(name.length));
    if (name !== undefined && match) result.push({name, version:Number(match[1]) as 1|2, default:prefix[1] === '*'});
  }
  if (result.length !== names.length || new Set(result.map(item => item.name.toLowerCase())).size !== result.length) throw new Error('WSL 目标列表不明确。');
  return result;
}

export function wslResourcePaths(root: string): DesktopResourcePaths {
  guestAbsolutePath(root);
  return {root, pythonExecutable:path.posix.join(root,'python/bin/python3'), playwrightBrowsers:path.posix.join(root,'playwright'), postgresBin:path.posix.join(root,'postgres/bin'), packageCli:path.posix.join(root,'bin/pulsara')};
}

export async function loadWslPayload(resourcesRoot: string): Promise<WslPayload> {
  const value: unknown = JSON.parse(await readFile(path.join(resourcesRoot, 'wsl-payload','manifest.json'), 'utf8'));
  if (!value || typeof value !== 'object') throw new Error('缺少 Windows 随包 Linux 资源信息。');
  const v = value as Record<string, unknown>;
  if (typeof v.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(v.sha256)
      || typeof v.version !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$/.test(v.version)
      || v.platform !== 'linux-x86_64') throw new Error('Linux 资源清单无效或架构不受支持。');
  return {archive:path.join(resourcesRoot,'wsl-payload','payload.tar'), sha256:v.sha256, version:v.version, interopHelper:path.join(resourcesRoot,'windows-interop','pulsara-interop.exe')};
}

const identityScript = `set -eu
uid=$(id -u)
user=$(id -un)
home=$(getent passwd "$uid" | cut -d: -f6)
home=$(realpath -e -- "$home")
test -d "$home"; test -w "$home"; test -x "$home"; test -O "$home"
fs=$(/usr/bin/findmnt -n -o FSTYPE -T "$home")
. /etc/os-release
printf '%s\\0' "$uid" "$user" "$home" "$fs" "$ID"
`;

// The archive is a packaged, digest-verified application resource, not a user
// upload. GNU tar owns extraction; build-time validation owns its fixed layout.
const installScript = `set -eu
umask 077
base="$1/.local/share/pulsara/runtime"
parent="$base"
while ! test -e "$parent" && ! test -L "$parent"; do parent="\${parent%/*}"; done
test -d "$parent"; test -O "$parent"
test "$(realpath -e -- "$parent")" = "$parent"
mkdir -p -- "$base"
test ! -L "$base"; test -O "$base"
test "$(realpath -e -- "$base")" = "$base"
test "$(/usr/bin/findmnt -n -o FSTYPE -T "$base")" = ext4
chmod 700 -- "$base"
exec 9>"$base/.install.lock"
# A previous finite installer may outlive its Windows host. Never queue a late publish.
flock -n 9 || { echo 'Another resource installation is still running.' >&2; exit 1; }
stage=$(mktemp -d "$base/.install-XXXXXXXX")
trap 'case "$stage" in "$base"/.install-*) rm -rf -- "$stage";; esac' EXIT
cat > "$stage/payload.tar"
printf '%s  %s\\n' "$3" "$stage/payload.tar" | sha256sum -c - >/dev/null
destination="$base/$2"
if test -e "$destination"; then
  test -d "$destination"; test ! -L "$destination"; test -O "$destination"
  test "$(cat "$destination/.pulsara-payload.sha256")" = "$3" || { echo 'This application version contains different resources; close the application and select an explicit update.' >&2; exit 1; }
else
  mkdir "$stage/unpack"
  tar -xf "$stage/payload.tar" -C "$stage/unpack" --no-same-owner --no-same-permissions
  test -x "$stage/unpack/payload/python/bin/python3"
  test -x "$stage/unpack/payload/postgres/bin/pg_ctl"
  "$stage/unpack/payload/python/bin/python3" -I -c "$4" "$stage/unpack/payload" >/dev/null
  printf '%s\\n' "$3" > "$stage/unpack/payload/.pulsara-payload.sha256"
  chmod 700 "$stage/unpack/payload"
  mv -T -- "$stage/unpack/payload" "$destination"
fi
"$destination/python/bin/python3" -I -c "$4" "$destination"
`;

const verifyInstalled = `import json,os,stat,subprocess,sys
from pathlib import Path
root=Path(sys.argv[1])
assert root.resolve()==root and root.stat().st_uid==os.getuid()!=0
import pulsara_agent
assert Path(pulsara_agent.__file__).resolve().is_relative_to(root)
for name in ('python/bin/python3','postgres/bin/postgres','postgres/bin/pg_ctl','postgres/bin/initdb','postgres/bin/pg_config','bin/pulsara'):
 p=root/name
 assert p.is_file() and os.access(p,os.X_OK) and p.resolve().is_relative_to(root), name
assert (root/'playwright').is_dir()
from pulsara_agent.capability.pulsara_home import require_pulsara_home
home=require_pulsara_home()
parent=home
while not parent.exists():parent=parent.parent
assert subprocess.check_output(['/usr/bin/findmnt','-n','-o','FSTYPE','-T',str(parent)],text=True).strip()=='ext4'
assert home.resolve()==home and home!=Path('/')
home.mkdir(mode=0o700,parents=True,exist_ok=True)
assert home.stat().st_uid==os.getuid() and stat.S_IMODE(home.stat().st_mode)&0o077==0
print(json.dumps({'root':str(root),'home':str(home),'uid':os.getuid()}))
`;

export class WslSetup {
  constructor(private readonly selectionFile: string) {}
  async discover(): Promise<{distros: WslDistribution[]; saved?: WslSelection; savedError?: string}> {
    const outputs = await Promise.all([['--list','--verbose'],['--list','--quiet']].map(args => execute(windowsWslExecutable(), args, {env:wslHostEnvironment(), windowsHide:true, shell:false, encoding:'buffer', maxBuffer:4*1024*1024})));
    let saved: WslSelection | undefined;
    let savedError: string | undefined;
    try {
      const value: unknown = JSON.parse(await readFile(this.selectionFile,'utf8'));
      if (!value || typeof value !== 'object' || Object.keys(value).sort().join(',') !== 'distro,user') throw new Error('已保存的 WSL 目标无效，请重新明确选择。');
      saved = value as WslSelection; validateWslSelection(saved);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') savedError = '已保存的 WSL 目标无法读取或无效，请重新明确选择。';
      saved = undefined;
    }
    return {distros:parseWslDistributions(outputs[0].stdout,outputs[1].stdout), ...(saved ? {saved} : {}), ...(savedError ? {savedError} : {})};
  }
  async inspect(selection: {distro: string; user?: string}): Promise<WslTarget> {
    validateWslSelection(selection);
    const found = (await this.discover()).distros.find(item => item.name === selection.distro);
    if (!found || found.version !== 2) throw new Error('请选择已存在的 WSL2 Ubuntu；不会新建或替换发行版。');
    if (selection.user !== undefined) {
      const observed = await executeWsl({distro:selection.distro},'/usr/bin/id',['-u','--',selection.user]);
      if (!/^[0-9]+\s*$/.test(observed.stdout) || Number(observed.stdout) <= 0) throw new Error('应用必须由所选非 root 用户运行。');
    }
    const {stdout} = await executeWsl(selection, '/bin/sh', ['-c',identityScript]);
    const parts = stdout.split('\0');
    if (parts.length !== 6 || parts[5] !== '' || !/^[0-9]+$/.test(parts[0]) || parts[3] !== 'ext4' || parts[4] !== 'ubuntu') throw new Error('目标必须是具有可写 ext4 用户目录的 Ubuntu。');
    const uid = Number(parts[0]);
    if (!Number.isSafeInteger(uid) || uid <= 0 || (selection.user !== undefined && parts[1] !== selection.user)) throw new Error('应用必须由所选非 root 用户运行。');
    guestAbsolutePath(parts[2]);
    return {distro:selection.distro,user:parts[1],uid,home:parts[2]};
  }
  async select(selection: WslSelection): Promise<WslTarget> {
    const target = await this.inspect(selection);
    await mkdir(path.dirname(this.selectionFile), {recursive:true});
    await writeFile(this.selectionFile+'.tmp', JSON.stringify({distro:target.distro,user:target.user}), {mode:0o600});
    await rename(this.selectionFile+'.tmp',this.selectionFile);
    return target;
  }
  async prepare(target: WslTarget, payload: WslPayload, options: {onProgress?: (message: string) => void; signal?: AbortSignal} = {}): Promise<WslPrepared> {
    const current = await this.inspect({distro:target.distro,user:target.user});
    if (current.uid !== target.uid || current.home !== target.home) throw new Error('WSL 用户身份或实际目录已改变，请重新确认。');
    if (!/^[a-f0-9]{64}$/.test(payload.sha256) || !/^[a-zA-Z0-9][a-zA-Z0-9.+_-]*$/.test(payload.version)) throw new Error('无效的随包资源信息。');
    options.signal?.throwIfAborted();
    options.onProgress?.('正在检查 Windows 互操作资源…');
    const helper = await execute(payload.interopHelper,['--version'],{env:wslHostEnvironment(),windowsHide:true,shell:false,encoding:'utf8'});
    if (helper.stdout.trim() !== 'pulsara-interop 1') throw new Error('Windows 互操作资源版本不匹配。');
    const hostHelperPath = await hostPathInWsl(current,payload.interopHelper);
    await executeWsl(current,'/usr/bin/test',['-x',hostHelperPath],current.home);
    const expected = path.posix.join(current.home,'.local/share/pulsara/runtime',payload.version);
    const existing = await executeWsl(current,'/bin/sh',['-c','if test -e "$1" || test -L "$1"; then printf present; else printf absent; fi','pulsara-resource-status',expected],current.home);
    if (existing.stdout === 'present') {
      options.onProgress?.('正在校验已安装的 Linux 应用资源…');
      await executeWsl(current,'/bin/sh',['-eu','-c','test ! -L "$1"; test "$(cat "$1/.pulsara-payload.sha256")" = "$2"','pulsara-resource-identity',expected,payload.sha256],current.home);
      const {stdout} = await executeWsl(current,path.posix.join(expected,'python/bin/python3'),['-I','-c',verifyInstalled,expected],current.home);
      const installed = JSON.parse(stdout) as {root:string;home:string;uid:number};
      if (installed.root !== expected || installed.uid !== current.uid || typeof installed.home !== 'string') throw new Error('已安装资源与所选目标不一致。');
      guestAbsolutePath(installed.home); options.signal?.throwIfAborted();
      return {target:current,home:installed.home,resources:wslResourcePaths(expected),hostHelperPath};
    }
    options.onProgress?.('正在校验并安装用户私有 Linux 应用资源…');
    // Node owns streaming/backpressure. Closing this input on cancellation lets
    // the finite Linux installer reject partial bytes and clean its own stage.
    options.signal?.throwIfAborted();
    const operation = execute(windowsWslExecutable(), wslArguments(current, '/bin/sh', ['-c',installScript,'pulsara-install',current.home,payload.version,payload.sha256,verifyInstalled], current.home), {env:wslHostEnvironment(), windowsHide:true, shell:false, encoding:'utf8', maxBuffer:4*1024*1024});
    const child = operation.child;
    const output = operation.then(result => result.stdout);
    void output.catch(() => {});
    if (!child.stdin) { await output; throw new Error('WSL 安装输入通道不可用。'); }
    const source = createReadStream(payload.archive);
    const cancel = () => source.destroy(options.signal?.reason instanceof Error ? options.signal.reason : new Error('资源安装已取消。'));
    options.signal?.addEventListener('abort',cancel,{once:true});
    let streamError: unknown;
    try { await pipeline(source,child.stdin); } catch (error) { streamError=error; }
    let raw = ''; let operationError: unknown;
    try { raw=await output; } catch (error) { operationError=error; } finally { options.signal?.removeEventListener('abort',cancel); }
    if (streamError && !(['EPIPE','ERR_STREAM_PREMATURE_CLOSE'].includes((streamError as NodeJS.ErrnoException).code ?? '') && operationError)) throw streamError;
    if (operationError) throw operationError;
    options.signal?.throwIfAborted();
    const result = JSON.parse(raw) as {root: string; home: string; uid: number};
    if (result.root !== expected || result.uid !== current.uid || typeof result.home !== 'string') throw new Error('Linux 资源安装结果与目标不一致。');
    guestAbsolutePath(result.home);
    return {target:current,home:result.home,resources:wslResourcePaths(expected),hostHelperPath};
  }
}
