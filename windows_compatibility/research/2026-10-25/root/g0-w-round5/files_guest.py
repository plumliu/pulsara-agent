"""G0-only finite directory GUI fixture; reuses production binding/import owners."""

import asyncio
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile

from aiohttp import web

sys.path.insert(0, "/home/plumwsl/workspaces/pulsara_agent/src")
from pulsara_agent.local_source_binding import open_absolute_directory_nofollow
from pulsara_agent.workspace_identity import HostWorkspaceInput, resolve_workspace
from pulsara_agent.desktop_app.file_import import LocalImport, receive_file

os.umask(0o077)
HOME = Path(tempfile.mkdtemp(prefix=".g0-files-", dir=Path.home()))
os.environ["PULSARA_HOME"] = str(HOME)
ROOT = Path.home() / "workspaces"
EVENTS = []
ORIGIN = ""


class DirectoryOutcomeUnknown(RuntimeError):
    pass


def emit(event, **values):
    value = {"event": event, **values}
    EVENTS.append(value)
    print(json.dumps(value, ensure_ascii=False), flush=True)


def path_input(raw):
    if raw.startswith("\\\\"):
        parts = raw.split("\\")
        if (
            len(parts) < 5
            or parts[2].lower() not in ("wsl.localhost", "wsl$")
            or parts[3] != "Ubuntu"
        ):
            raise ValueError("wrong UNC target")
        raw = "/" + "/".join(parts[4:])
    if (
        not raw.startswith("/")
        or "\\" in raw
        or "\x00" in raw
        or any(part in ("", ".", "..") for part in raw[1:].split("/"))
    ):
        raise ValueError("not a canonical Linux workspace path")
    path = Path(raw)
    if not path.is_relative_to(ROOT):
        raise ValueError("outside workspace root")
    return path


def bind(raw):
    path = path_input(raw)
    fd = open_absolute_directory_nofollow(path)
    try:
        mount = json.loads(
            subprocess.check_output(
                [
                    "findmnt",
                    "-J",
                    "-T",
                    f"/proc/self/fd/{fd}",
                    "-o",
                    "TARGET,FSTYPE,SOURCE",
                ],
                pass_fds=(fd,),
                text=True,
            )
        )["filesystems"][0]
        if mount["fstype"] != "ext4" or Path(mount["target"]).is_relative_to(ROOT):
            raise ValueError(
                "workspace must be native ext4, without a replacement mount"
            )
        if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
            raise PermissionError("workspace permissions")
        check = open_absolute_directory_nofollow(path)
        try:
            if not os.path.samestat(os.fstat(fd), os.fstat(check)):
                raise ValueError("directory changed")
        finally:
            os.close(check)
        return path, fd, mount
    except BaseException:
        os.close(fd)
        raise


def leaf(raw):
    if not isinstance(raw, str) or not raw or raw in (".", "..") or raw[-1] in " .":
        raise ValueError("invalid single name")
    if any(ord(c) < 32 or c in '<>:"/\\|?*' for c in raw):
        raise ValueError("invalid Windows-visible name")
    if raw.split(".")[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        raise ValueError("reserved Windows-visible name")
    return raw


@web.middleware
async def boundary(request, handler):
    if request.host != ORIGIN.removeprefix("https://"):
        raise web.HTTPMisdirectedRequest()
    if request.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin"):
        raise web.HTTPForbidden()
    if request.method == "POST" and request.headers.get("Origin") not in (None, ORIGIN):
        raise web.HTTPForbidden()
    try:
        return await handler(request)
    except DirectoryOutcomeUnknown as exc:
        emit("unknown", route=request.path, detail=str(exc))
        return web.json_response(
            {"outcome": "unknown", "detail": str(exc), "retry": False}, status=409
        )
    except (ValueError, OSError) as exc:
        emit("rejected", route=request.path, error=type(exc).__name__, detail=str(exc))
        return web.json_response(
            {"error": type(exc).__name__, "detail": str(exc)}, status=400
        )


async def directories(request):
    data = await request.json()
    path, fd, mount = bind(data["path"])
    try:
        op = data["op"]
        if op == "list":
            # 100 is the existing directory page size, not a total-directory cap.
            names = sorted(
                name for name in os.listdir(fd) if os.path.isdir(path / name)
            )
            start = int(data.get("offset", 0))
            if start < 0:
                raise ValueError("invalid page offset")
            result = {
                "path": str(path),
                "names": names[start : start + 100],
                "next": start + 100 if len(names) > start + 100 else None,
            }
        elif op == "mkdir":
            name = leaf(data["name"])
            os.mkdir(name, 0o700, dir_fd=fd)
            try:
                target, child, _ = bind(str(path / name))
                try:
                    if not os.path.samestat(
                        os.stat(name, dir_fd=fd, follow_symlinks=False), os.fstat(child)
                    ):
                        raise ValueError("new directory changed")
                    parent = open_absolute_directory_nofollow(path)
                    try:
                        if not os.path.samestat(os.fstat(parent), os.fstat(fd)):
                            raise ValueError("parent changed after mkdir")
                    finally:
                        os.close(parent)
                    result = {"created": str(target)}
                finally:
                    os.close(child)
            except (ValueError, OSError) as exc:
                raise DirectoryOutcomeUnknown(
                    f"mkdir was submitted; refresh before another action: {exc}"
                ) from exc
        elif op == "select":
            resolved = resolve_workspace(HostWorkspaceInput("project", path))
            _, current, _ = bind(str(path))
            try:
                if not os.path.samestat(os.fstat(current), os.fstat(fd)):
                    raise ValueError("directory changed before acceptance")
            finally:
                os.close(current)
            result = {
                "selected": str(resolved.workspace_root),
                "key": resolved.workspace_key,
                "activeSession": None,
            }
        else:
            raise ValueError("unknown directory operation")
        emit("directory", operation=op, result=result, mount=mount)
        return web.json_response(result)
    finally:
        os.close(fd)


async def upload(request):
    pending = LocalImport()
    try:
        name = await receive_file(request, pending)
        result = pending.publish(name)
        emit("imported", **result)
        return web.json_response(result)
    finally:
        pending.close()


async def index(request):
    return web.Response(
        text=HTML.replace("__ROOT__", json.dumps(str(ROOT))), content_type="text/html"
    )


HTML = r"""<!doctype html><html lang="zh"><meta charset="utf-8"><title>G0 Windows WSL2 文件验收</title>
<style>body{font:18px system-ui;margin:32px;background:#f2f5f9;color:#18283b}button,input{font:inherit;margin:6px;padding:9px}li{margin:7px}#status{white-space:pre-wrap;background:white;padding:16px}#drop{border:2px dashed #3474a5;padding:24px}select{font:inherit;min-width:550px;height:170px}</style>
<h1>Windows → WSL2 文件验收</h1><p>独立 G0 实验 · 没有 active session · Ubuntu / plumwsl</p>
<label>当前目录 <input id="path" size="65"></label><button id="refresh">刷新目录</button><button id="up">上一级</button><br>
<select id="dirs" size="6" aria-label="Linux目录"></select><button id="browse">进入目录</button><br>
<label>文件夹名称 <input id="name" aria-label="文件夹名称"></label><button id="mkdir">新建文件夹</button><button id="select">选择当前工作区</button><button id="cancel">取消选择</button>
<h2>Windows 文件导入</h2><input id="file" type="file" aria-label="选择 Windows 文件"><div id="drop">将 Windows 文件拖到这里，复制到独立 Linux 实验 home</div>
<pre id="status" role="status">准备目录服务</pre><script>
const root=__ROOT__, p=document.querySelector('#path'),s=document.querySelector('#status'),d=document.querySelector('#dirs');p.value=root;
window.evidence=[];function log(v){evidence.push(v);s.textContent=JSON.stringify(v,null,2)}
async function call(op,extra={}){const r=await fetch('/directories',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({op,path:p.value,...extra})});const v=await r.json();log(v);return v}
async function refresh(){const v=await call('list');d.replaceChildren();for(const n of v.names||[]){const o=document.createElement('option');o.textContent=n;o.value=n;d.append(o)}}
refresh.onclick=refresh;document.querySelector('#refresh').onclick=refresh;
document.querySelector('#up').onclick=()=>{if(p.value!==root){p.value=p.value.slice(0,p.value.lastIndexOf('/'));refresh()}};
document.querySelector('#browse').onclick=()=>{if(d.value){p.value+='/'+d.value;refresh()}};
document.querySelector('#mkdir').onclick=async()=>{await call('mkdir',{name:document.querySelector('#name').value});await refresh()};
document.querySelector('#select').onclick=()=>call('select');document.querySelector('#cancel').onclick=()=>log({cancelled:true,activeSession:null});
// Same ASCII JSON filename encoding as frontend/lib/runtime-adapter.ts::importFiles.
async function upload(f){try{const filename=JSON.stringify(f.name).replace(/[^\x20-\x7e]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));const r=await fetch('/import',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Pulsara-Filename':filename},body:f});log(await r.json())}catch(e){log({error:String(e)})}}
document.querySelector('#file').onchange=e=>upload(e.target.files[0]);const drop=document.querySelector('#drop');drop.ondragover=e=>e.preventDefault();drop.ondrop=e=>{e.preventDefault();for(const f of e.dataTransfer.files)upload(f)};refresh();
</script></html>"""


async def main():
    global ORIGIN
    key, cert = HOME / "tls.key", HOME / "tls.crt"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    public = cert.read_text()
    key.unlink()
    cert.unlink()
    app = web.Application(middlewares=[boundary])
    app.router.add_get("/", index)
    app.router.add_post("/directories", directories)
    app.router.add_post("/import", upload)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
    await site.start()
    ORIGIN = f"https://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    emit(
        "ready",
        origin=ORIGIN,
        certificate=public,
        home=str(HOME),
        root=str(ROOT),
        pid=os.getpid(),
        uid=os.getuid(),
    )
    await asyncio.to_thread(sys.stdin.buffer.read)
    await runner.cleanup()
    emit(
        "stopped",
        home=str(HOME),
        imports=[str(p) for p in (HOME / "imports").glob("*/*")]
        if (HOME / "imports").exists()
        else [],
    )


if __name__ == "__main__":
    asyncio.run(main())
