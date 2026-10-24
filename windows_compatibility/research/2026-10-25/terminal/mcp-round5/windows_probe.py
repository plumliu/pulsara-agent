"""Reuse the round-four exact Win32 scope around a real MCP owner process."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback
import uuid

BASE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location(
    "win_scope_fixture", BASE.parent / "round4" / "windows_probe.py"
)
W = importlib.util.module_from_spec(spec)
spec.loader.exec_module(W)


def run_case(scenario, candidate):
    label = (
        ("candidate-" if candidate else "baseline-")
        + scenario
        + "-"
        + uuid.uuid4().hex[:8]
    )
    prefix = BASE / label
    pe_record = Path(str(prefix) + ".pe.jsonl")
    diagnostic_record = Path(str(prefix) + ".diagnostic.jsonl")
    late_record = Path(str(prefix) + ".late-pe.jsonl")
    mcp_record = Path(str(prefix) + ".mcp.jsonl")
    configuration_path = Path(str(prefix) + ".config.json")
    scope_configuration_path = Path(str(prefix) + ".scope-config.json")
    job_name = "Local\\PulsaraG0Mcp-" + uuid.uuid4().hex
    pe_args = [W.lp(sys.executable), str(W.BASE / "windows_probe.py"), "pe"]
    workload = "normal" if scenario == "normal" else "cancel"
    scope_configuration = dict(
        name="mcp-session",
        directory="/home/plumwsl/.g0m5-" + uuid.uuid4().hex[:12],
        prefix=W.lp(prefix),
        windows_prefix=str(prefix),
        record=W.lp(str(prefix) + ".relay.jsonl"),
        windows_relay_record=str(prefix) + ".relay.jsonl",
        anchor_record=W.lp(str(prefix) + ".anchor.jsonl"),
        windows_anchor_record=str(prefix) + ".anchor.jsonl",
        fixture=W.lp(W.FIXTURE),
        config_path=W.lp(scope_configuration_path),
        gates=[],
        job_name=job_name,
        windows_record=str(prefix) + ".windows.jsonl",
        pe_record=W.lp(pe_record),
        windows_pe_record=str(pe_record),
        windows_diagnostic_record=str(diagnostic_record),
        diagnostic_args=pe_args
        + [str(diagnostic_record), job_name, "diagnostic", workload, str(prefix)],
        late_record=W.lp(late_record),
        late_args=pe_args + [str(late_record), job_name, "late", workload, str(prefix)],
        task_args=pe_args + [str(pe_record), job_name, "parent", workload, str(prefix)],
    )
    scope_configuration_path.write_text(
        json.dumps(scope_configuration), encoding="utf-8"
    )
    config = dict(
        candidate=candidate,
        scenario=scenario,
        scope=scope_configuration,
        mcp_record=W.lp(mcp_record),
        driver_prefix=W.lp(prefix),
        server_script=W.lp(BASE / "mcp_server.py"),
        server_record=W.lp(str(prefix) + ".server.jsonl"),
        task_args=scope_configuration["task_args"],
    )
    configuration_path.write_text(json.dumps(config), encoding="utf-8")
    scope = W.WindowsScope(scope_configuration)
    result = dict(label=label, scenario=scenario, candidate=candidate)
    process = subprocess.Popen(
        W.guest(W.lp(BASE / "mcp_probe.py"), W.lp(configuration_path)),
        env=W.ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 50
        captured = False
        while process.poll() is None:
            scope.tick()
            if Path(str(prefix) + ".observation-ready").exists() and not captured:
                result["windowsAtMcpOwnerClose"] = scope.facts()
                Path(str(prefix) + ".observation-captured").touch()
                captured = True
            if time.monotonic() > deadline:
                raise TimeoutError("MCP G0 diagnostic observation deadline")
            time.sleep(0.005)
        output, error = process.communicate()
        result["ownerProcess"] = dict(
            exit=process.returncode,
            stdout=output.decode(errors="replace"),
            stderr=error.decode(errors="replace"),
        )
        result["mcpRows"] = W.J.rows(mcp_record)
        result["serverRows"] = W.J.rows(Path(str(prefix) + ".server.jsonl"))
        result["relayRows"] = W.J.rows(Path(str(prefix) + ".relay.jsonl"))
        result["anchorRows"] = W.J.rows(Path(str(prefix) + ".anchor.jsonl"))
        result["windowsRows"] = scope.events
        result["peRows"] = W.J.rows(pe_record)
        assert process.returncode == 0, result["ownerProcess"]
        assert any(row["event"] == "probe-finished" for row in result["mcpRows"])
        result["observed"] = True
    except BaseException:
        result["failure"] = traceback.format_exc()
        scope.path(".cancel").touch()
        scope.path(".job-cancel").touch()
        deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < deadline:
            scope.tick()
            time.sleep(0.01)
        if process.poll() is not None:
            output, error = process.communicate()
            result["failedOwnerProcess"] = dict(
                exit=process.returncode,
                stdout=output.decode(errors="replace"),
                stderr=error.decode(errors="replace"),
            )
        else:
            result["ownerStillRunning"] = True
    finally:
        result["cleanupWindows"] = scope.cleanup()
        (BASE / (label + "-summary.json")).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps(
                {
                    key: result.get(key)
                    for key in (
                        "label",
                        "observed",
                        "failure",
                        "ownerProcess",
                        "failedOwnerProcess",
                        "ownerStillRunning",
                    )
                }
            ),
            flush=True,
        )
    return result


if __name__ == "__main__":
    group = sys.argv[1] if len(sys.argv) > 1 else "baseline"
    if group == "baseline":
        cases = [("normal", False)]
    elif group == "normal":
        cases = [("normal", True)]
    elif group == "failure":
        cases = [(name, True) for name in ("call-cancel", "timeout", "server-crash")]
    elif group == "guard":
        cases = [("guard-deny", True)]
    else:
        raise ValueError(group)
    results = [run_case(*case) for case in cases]
    (BASE / (group + "-" + uuid.uuid4().hex[:8] + "-results.json")).write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
