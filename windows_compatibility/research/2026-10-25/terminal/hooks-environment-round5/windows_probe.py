"""Output-only Hook/environment driver; reuse the reviewed round4 Job owner."""

import importlib.util
import json
import os  # noqa: F401 -- preserve executed research snapshot
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

BASE = Path(__file__).resolve().parent
ROOT = next(parent for parent in BASE.parents if (parent / "pyproject.toml").exists())
spec = importlib.util.spec_from_file_location(
    "hook_win_fixture",
    ROOT / "output/windows-g0/interop_tasks_r3/round4/windows_probe.py",
)
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)
J = W.J


def pe(config_path, role):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    cfg = json.loads(Path(config_path).read_text())
    record = (
        cfg["windows_diagnostic_record"]
        if role == "diagnostic"
        else cfg["windows_pe_record"]
    )
    job = J.check(J.K.OpenJobObjectW(4, False, cfg["job_name"]))
    in_job = J.member(J.K.GetCurrentProcess(), job)
    J.K.CloseHandle(job)
    J.emit(
        record,
        "pe-start",
        role=role,
        creation=J.creation(J.K.GetCurrentProcess()),
        inJob=in_job,
    )
    if role == "diagnostic":
        W.wait_until(
            lambda: Path(cfg["windows_prefix"] + ".diagnostic-accepted").exists()
        )
        return
    if role == "parent":
        raw = sys.stdin.buffer.read()
        J.emit(
            record,
            "public-stdin-received",
            bytes=len(raw),
            credentialMarker=b"[REDACTED_CREDENTIAL]" in raw,
            publicUnicode="中文".encode() in raw,
        )
        child = subprocess.Popen(
            [sys.executable, __file__, "pe", config_path, "child"],
            stdin=subprocess.DEVNULL,
            stdout=sys.stdout if cfg["hold_pipe"] else subprocess.DEVNULL,
            stderr=sys.stderr if cfg["hold_pipe"] else subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        W.wait_until(
            lambda: any(row["event"] == "child-ready" for row in J.rows(Path(record)))
        )
        if cfg["scenario"] in (
            "normal",
            "nonzero",
            "cancel-tail",
            "host-close",
            "environment",
        ):
            if not cfg.get("silent"):
                print("hook 中文✓", flush=True)
                print("hook stderr✓", file=sys.stderr, flush=True)
            J.emit(
                record,
                "parent-early-exit",
                childPid=child.pid,
                childHoldsPipe=cfg["hold_pipe"],
            )
            sys.exit(7 if cfg["scenario"] == "nonzero" else 0)
        child.wait()
    else:
        J.emit(record, "child-ready")
        time.sleep(cfg["child_seconds"])
    J.emit(record, "pe-finished", role=role)


def run_case(kind, scenario, *, patched, hold_pipe=False, seconds=2, timeout=8):
    label = (
        f"{kind}-{'scope' if patched else 'baseline'}-{scenario}-"
        + uuid.uuid4().hex[:8]
    )
    prefix = BASE / label
    config_path = Path(str(prefix) + ".config.json")
    cfg = dict(
        name="hook",
        kind=kind,
        scenario=scenario,
        patched=patched,
        hold_pipe=hold_pipe,
        child_seconds=seconds,
        timeout=timeout,
        silent=kind == "environment",
        prefix=W.lp(prefix),
        windows_prefix=str(prefix),
        directory="/home/plumwsl/.g0h5-" + uuid.uuid4().hex[:12],
        env_home="/home/plumwsl/.g0h5env-" + uuid.uuid4().hex[:12],
        record=W.lp(str(prefix) + ".relay.jsonl"),
        windows_relay_record=str(prefix) + ".relay.jsonl",
        anchor_record=W.lp(str(prefix) + ".anchor.jsonl"),
        windows_anchor_record=str(prefix) + ".anchor.jsonl",
        fixture=W.lp(
            ROOT / "output/windows-g0/interop_races_r3/round4/relay_fixture.py"
        ),
        config_path=W.lp(config_path),
        windows_config_path=str(config_path),
        job_name="Local\\PulsaraG0Hook5-" + label,
        windows_record=str(prefix) + ".windows.jsonl",
        windows_pe_record=str(prefix) + ".pe.jsonl",
        windows_diagnostic_record=str(prefix) + ".diagnostic.jsonl",
        late_record=W.lp(str(prefix) + ".late.jsonl"),
        gates=[],
        owner_record=W.lp(str(prefix) + ".owner.jsonl"),
    )
    cfg["diagnostic_args"] = [
        W.lp(sys.executable),
        str(Path(__file__).resolve()),
        "pe",
        str(config_path),
        "diagnostic",
    ]
    cfg["late_args"] = [
        W.lp(sys.executable),
        str(Path(__file__).resolve()),
        "pe",
        str(config_path),
        "late",
    ]
    cfg["pe_args"] = [
        W.lp(sys.executable),
        str(Path(__file__).resolve()),
        "pe",
        str(config_path),
        "parent",
    ]
    config_path.write_text(json.dumps(cfg), encoding="utf-8")
    scope = W.WindowsScope(cfg)
    process = subprocess.Popen(
        W.guest(W.lp(BASE / "guest_probe.py"), W.lp(config_path)),
        env=W.ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    result = dict(
        label=label,
        config=cfg,
        head=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
    )
    try:
        deadline = time.monotonic() + 65
        while process.poll() is None:
            scope.tick()
            if time.monotonic() > deadline:
                raise TimeoutError("finite experiment observation deadline")
            time.sleep(0.005)
        out, err = process.communicate()
        result["guest"] = dict(
            exit=process.returncode,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )
        result["ownerRows"] = J.rows(Path(str(prefix) + ".owner.jsonl"))
        result["relayRows"] = J.rows(Path(cfg["windows_relay_record"]))
        result["anchorRows"] = J.rows(Path(cfg["windows_anchor_record"]))
        result["peRows"] = J.rows(Path(cfg["windows_pe_record"]))
        result["diagnosticRows"] = J.rows(Path(cfg["windows_diagnostic_record"]))
        result["windowsEvents"] = scope.events
        assert process.returncode == 0, result["guest"]
        result["observed"] = True
    except BaseException:
        result["failure"] = traceback.format_exc()
        scope.path(".cancel").touch()
        scope.path(".job-cancel").touch()
        until = time.monotonic() + 15
        while process.poll() is None and time.monotonic() < until:
            scope.tick()
            time.sleep(0.01)
        if process.poll() is None:
            result["guestStillRunning"] = True
        else:
            out, err = process.communicate()
            result["failureGuest"] = dict(
                exit=process.returncode,
                stdout=out.decode(errors="replace"),
                stderr=err.decode(errors="replace"),
            )
    finally:
        result["cleanupWindows"] = scope.cleanup()
        path = BASE / (label + "-summary.json")
        path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                dict(
                    label=label,
                    observed=result.get("observed"),
                    failure=result.get("failure"),
                    guest=result.get("guest"),
                    guestStillRunning=result.get("guestStillRunning"),
                )
            ),
            flush=True,
        )
    return result


def main(group):
    if group == "baseline":
        cases = [
            ("hook", "normal", dict(patched=False, seconds=5)),
            (
                "hook",
                "normal",
                dict(patched=False, hold_pipe=True, seconds=4, timeout=2),
            ),
            ("environment", "environment", dict(patched=False, seconds=5, timeout=2)),
        ]
    elif group == "hooks":
        cases = [
            ("hook", "normal", dict(patched=True)),
            ("hook", "nonzero", dict(patched=True)),
            (
                "hook",
                "timeout",
                dict(patched=True, hold_pipe=True, seconds=20, timeout=3),
            ),
            ("hook", "cancel-tail", dict(patched=True, seconds=20)),
            ("hook", "task-cancel", dict(patched=True, hold_pipe=True, seconds=20)),
            ("hook", "host-close", dict(patched=True, seconds=20)),
        ]
    elif group == "guard":
        cases = [("hook", "guard", dict(patched=True))]
    elif group == "environment":
        cases = [
            ("environment", "environment", dict(patched=True, seconds=20, timeout=2)),
            (
                "environment",
                "close",
                dict(patched=True, hold_pipe=True, seconds=20, timeout=8),
            ),
        ]
    elif group == "smoke":
        cases = [("hook", "normal", dict(patched=True))]
    else:
        raise ValueError(group)
    results = [run_case(kind, scenario, **kwargs) for kind, scenario, kwargs in cases]
    path = BASE / (group + "-" + uuid.uuid4().hex[:8] + "-results.json")
    path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "pe":
        pe(*sys.argv[2:])
    else:
        main(sys.argv[1] if len(sys.argv) > 1 else "smoke")
