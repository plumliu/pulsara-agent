"""Test gates around the installed helper, with its actual implementation intact."""
import argparse
import inspect
import json
import os
from pathlib import Path
import signal
import sys

import pulsara_agent.desktop_guest_ops as helper

parser = argparse.ArgumentParser()
parser.add_argument("--config", type=Path, required=True)
parser.add_argument("--invocation", required=True)
parser.add_argument("--armed", choices=("yes", "no"), required=True)
options, original = parser.parse_known_args()
config = json.loads(options.config.read_text())
home = Path(config["home"])
assert Path(os.environ["PULSARA_HOME"]) == home
assert str(Path(helper.__file__)).startswith(config["installed"] + "/python/")
assert Path(helper.__file__).read_bytes() == Path(config["repo"], "src/pulsara_agent/desktop_guest_ops.py").read_bytes()
method = original[original.index("--method") + 1]
armed = options.armed == "yes" and method == config["method"]

def actor(pid):
    path = Path("/proc") / str(pid)
    return {"pid": pid, "uid": path.stat().st_uid,
            "start_ticks": path.joinpath("stat").read_text().rsplit(")", 1)[1].split()[19]}

info = {**actor(os.getpid()), "method": method, "armed": armed, "helper": helper.__file__}
(home / f"helper-{options.invocation}.json").write_text(json.dumps(info))
original_popen = helper.subprocess.Popen

def popen(*args, **kwargs):
    child = original_popen(*args, **kwargs)
    inherited = [{"fd": fd, "inode": os.fstat(fd).st_ino} for fd in kwargs.get("pass_fds", ())]
    event = {**actor(child.pid), "argv": args[0], "inherited": inherited}
    with (home / f"children-{options.invocation}.jsonl").open("a") as stream:
        stream.write(json.dumps(event) + "\n")
    return child

helper.subprocess.Popen = popen
emit = helper._emit

def gate(name):
    path = home / name
    with path.open("x") as stream:
        json.dump(actor(os.getpid()), stream)
    signal.raise_signal(signal.SIGSTOP)

def emitting(value):
    if armed and config.get("before_ready") and value.get("type") == "ready":
        gate("gate-ready")
    if armed and config.get("before_failed_result") and value.get("type") == "result" and not value.get("ok") and value["error"]["execution"] == "failed":
        gate("gate-failed-result")
    return emit(value)

helper._emit = emitting
if armed and config.get("native_stage"):
    os.environ.update(LD_PRELOAD=config["library"], PULSARA_TEST_STAGE=config["native_stage"], PULSARA_TEST_HOME=str(home))
if armed and config.get("readiness_timeout"):
    os.environ["PGCTLTIMEOUT"] = "1"
sys.argv = [inspect.getfile(helper), *original]
helper.main()
