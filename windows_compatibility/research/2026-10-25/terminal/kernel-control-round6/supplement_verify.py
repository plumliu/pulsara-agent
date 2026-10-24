"""Offline checks for the selected distro/signal/window records; sleep stays pending."""

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent


def read(name):
    return json.loads((BASE / name).read_text(encoding="utf-8"))


def event(case, name):
    return [row for row in case["hostRows"] if row.get("event") == name]


def kernel(case, name):
    return [row for row in case["kernelRows"] if row["event"] == name]


cases = []
checks = []
for suffix, policy in (
    ("b772f4da", "observe-default"),
    ("fb2f58fa", "ignore-hup"),
    ("61abedf7", "ignore-hup"),
):
    path = next(BASE.glob("*" + suffix + "-summary.json"))
    case = read(path.name)
    case["sourceSummary"] = path.name
    case["hostResult"] = {
        key: value for key, value in case.get("hostResult", {}).items() if key != "rows"
    }
    signals = kernel(case, "actual-posix-signal")
    assert signals and all(
        row["signum"] == 1 and row["policy"] == policy for row in signals
    )
    assert case["hostKilledThroughExactHandle"] and case["hostExit"] == 93
    assert case["pidfdObserver"]["rows"][-1]["signalled"]
    if policy == "observe-default":
        assert not kernel(case, "original-application-close-entered")
    else:
        assert case["afterHostDeath"]["kernel"]["alive"]
        assert (
            case["afterHostDeath"]["closeGateEntered"]
            and not case["afterHostDeath"]["lock"]["acquired"]
        )
        assert kernel(case, "original-application-close-returned")
    assert case["beforeCleanup"]["postgres"]["exit"] == 0
    cases.append(case)
    checks.append(
        dict(
            label=case["label"],
            claim="actual-sighup-default-vs-startup-ignore-original-eof-close",
        )
    )

positive = read("normal-3cf88beb-result.json")
stop = event(positive, "before-original-pg-stop")[0]
assert (
    kernel(positive, "original-application-close-returned")[0]["time"] * 1000
    <= stop["time"]
)
assert not stop["state"]["kernel"]["alive"] and stop["state"]["lock"]["acquired"]
assert (
    positive["hostResult"]["pgStopCalls"] == 1
    and positive["hostResult"]["appExit"] == 0
)
cases.append(positive)
checks.append(
    dict(
        label=positive["label"],
        claim="fresh-official-distro-real-bundle-normal-control",
    )
)

loss = read("distro-loss-ae39b6d0-result.json")
before, after = loss["beforeTerminate"], loss["afterExplicitRestart"]
assert loss["heldCarrier"]["waitAfterTerminate"] == 0
assert before["pidNamespace"] != after["pidNamespace"]
assert not before["kernelLock"]["acquired"] and not before["pgLock"]["acquired"]
assert (
    after["kernelLock"]["acquired"]
    and after["pgLock"]["acquired"]
    and not after["ownedPgProcesses"]
)
assert before["pgDataInode"] == after["pgDataInode"]
assert before["kernelLock"]["inode"] == after["kernelLock"]["inode"]
assert before["pgLock"]["inode"] == after["pgLock"]["inode"]
assert not kernel(loss, "original-application-close-returned")
assert loss["hostResult"]["pgStopCalls"] == 0 and loss["hostResult"]["appExit"] is None
assert loss["hostResult"]["quitError"]
assert loss["cleanup"]["before"]["namespaceDiscontinued"]
cases.append(loss)
checks.append(
    dict(
        label=loss["label"],
        claim="whole-distro-unclean-loss-exact-carrier-join-new-namespace-only-no-replay",
    )
)

for suffix in ("200378a3", "1f068d6c"):
    path = next(BASE.glob("*" + suffix + "-summary.json"))
    case = read(path.name)
    case["sourceSummary"] = path.name
    if suffix == "200378a3":
        case["supplementVerificationFailure"] = read(
            "browser-probe-200378a3-failure.json"
        )
        case["qualified"] = False
    else:
        case["hostResult"].pop("rows", None)
        assert event(case, "actual-browser-window-visible")[0]["visible"]
        close = event(case, "actual-browser-window-close-event")[0]
        stop = event(case, "before-original-pg-stop")[0]
        assert close["time"] <= stop["time"]
        assert (
            kernel(case, "original-application-close-returned")[0]["time"] * 1000
            <= stop["time"]
        )
        assert (
            not stop["state"]["kernel"]["alive"] and stop["state"]["lock"]["acquired"]
        )
        assert (
            case["hostResult"]["pgStopCalls"] == 1
            and case["hostResult"]["appExit"] == 0
        )
        case["qualified"] = True
        checks.append(
            dict(
                label=case["label"],
                claim="actual-visible-window-original-close-and-quit-join-before-pg-stop",
            )
        )
    cases.append(case)

sleep_files = [
    path for path in BASE.glob("sleep-*-result.json") if ".host-result" not in path.name
]
if sleep_files:
    assert len(sleep_files) == 1
    case = read(sleep_files[0].name)
    assert case["observed"]
    assert case["beforeSleep"] == case["afterSleep"]
    assert case["hostResult"]["pgStopCalls"] == 1 and case["hostResult"]["appExit"] == 0
    stop = event(case, "before-original-pg-stop")[0]
    carrier = event(case, "actual-wsl-carrier-close")[0]
    assert (
        kernel(case, "original-application-close-returned")[0]["time"] * 1000
        <= carrier["time"]
        <= stop["time"]
    )
    assert carrier["code"] == 0 and carrier["signal"] is None
    assert not stop["state"]["kernel"]["alive"] and stop["state"]["lock"]["acquired"]
    cases.append(case)
    checks.append(
        dict(
            label=case["label"],
            claim="same-real-kernel-pg-locks-sql-after-root-confirmed-human-resume-and-normal-quit",
        )
    )
cleanup = read("distro-cleanup.json")
assert (
    cleanup["ownRegistrationAbsent"]
    and cleanup["ownFolderAbsent"]
    and cleanup["sharedUbuntuStillRunning"]
)
assert all(
    not case["cleanup"]["homeExists"]
    and not case["cleanup"]["forcedExactFixtureTeardown"]
    for case in cases
)
(BASE / "supplement-matrix.json").write_text(
    json.dumps(
        dict(
            cases=cases,
            checks=checks,
            sleep="completed" if sleep_files else "pending-human-resume",
            distroCleanup=cleanup,
        ),
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print(
    json.dumps(
        dict(
            cases=len(cases),
            narrowChecks=len(checks),
            sleep="completed" if sleep_files else "pending-human-resume",
        )
    )
)
