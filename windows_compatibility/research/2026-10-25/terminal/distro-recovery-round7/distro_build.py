"""Mechanical output-only target adapters; preserve original protocol/owners."""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[3]
config = json.loads((BASE / "distro-state.json").read_text(encoding="utf-8"))
pg = ROOT / "output/windows-g0/pg_admission_r3/round5"
target = BASE / "distro-pg"
target.mkdir(exist_ok=True)
for name in ("helper.py", "control.py"):
    source = (pg / name).read_text(encoding="utf-8")
    if name == "control.py":
        source = source.replace("import stat\n", "")
    else:
        source = source.replace(
            "from pulsara_agent.desktop_runtime import acquire_kernel_lock",
            "from pulsara_agent.desktop_runtime import acquire_kernel_lock  # noqa: E402",
        )
    (target / name).write_text(source, encoding="utf-8")


def once(source, before, after):
    assert source.count(before) == 1, before
    return source.replace(before, after)


client = (pg / "client.cjs").read_text(encoding="utf-8")
client = once(
    client,
    "/output/windows-g0/pg_admission_r3/round5",
    "/output/windows-g0/interop_tasks_r3/round7/distro-pg",
)
client = once(
    client,
    "repo + '/.venv/bin/python'",
    "repo + '/desktop/resources/python/bin/python3'",
)
client = once(client, "'-d', 'Ubuntu'", "'-d', " + json.dumps(config["name"]))
(BASE / "distro-client.cjs").write_text(client, encoding="utf-8")

guest = (BASE / "guest_control.py").read_text(encoding="utf-8")
guest = once(
    guest,
    'str(REPO / ".venv/bin/python")',
    'str(REPO / "desktop/resources/python/bin/python3")',
)
guest = once(
    guest,
    "output/windows-g0/pg_admission_r3/round5/control.py",
    "output/windows-g0/interop_tasks_r3/round7/distro-pg/control.py",
)
guest = once(
    guest,
    '    path = Path("/proc") / str(identity["pid"]) / "stat"',
    '''    namespace = os.readlink("/proc/self/ns/pid")
    if identity["pidNamespace"] != namespace:
        return dict(**identity, namespaceDiscontinued=True, currentPidNamespace=namespace, sameIdentity=False, alive=False)
    path = Path("/proc") / str(identity["pid"]) / "stat"''',
)
(BASE / "distro_guest_control.py").write_text(guest, encoding="utf-8")

site = BASE / "distro-site"
site.mkdir(exist_ok=True)
source = (BASE / "sitecustomize.py").read_text(encoding="utf-8")
source = once(
    source,
    "pid=os.getpid(),",
    'pid=os.getpid(), pidNamespace=os.readlink("/proc/self/ns/pid"),',
)
(site / "sitecustomize.py").write_text(source, encoding="utf-8")

owner = (BASE / "owner.cjs").read_text(encoding="utf-8")
owner = once(
    owner,
    "require(path.join(repo, 'output/windows-g0/pg_admission_r3/round5/client.cjs'))",
    "require('./distro-client.cjs')",
)
owner = once(owner, "linux + '/.venv/bin/python'", "bundledPython")
owner = once(owner, "'-d', 'Ubuntu'", "'-d', " + json.dumps(config["name"]))
owner = once(
    owner,
    "    if (config.scenario === 'host-loss') {",
    """    if (config.scenario === 'distro-loss') {
        assert(origin);
        emit('temporary-distro-terminate-ready');
        await until(() => runtime.child === undefined, 120);
        emit('temporary-distro-carrier-closed-before-restart', {runtimeRunning: runtime.running});
        await until(() => fs.existsSync(config.resumeObservationPath), 120);
    }
    if (config.scenario === 'host-loss') {""",
)
(BASE / "distro-owner.cjs").write_text(owner, encoding="utf-8")
print(
    json.dumps(
        dict(
            target=config["name"],
            pgProtocol="mechanical-copy-same-framing",
            owner="original-runtime-and-quit-plus-r6-two-line-candidate",
        )
    )
)
