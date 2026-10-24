"""Exact host death with real signal evidence and the original EOF/close owner."""

import json
import sys

import windows_probe as P

policy = sys.argv[1]
electron = len(sys.argv) > 2 and sys.argv[2] == "electron"
result = P.run_case(
    "host-loss",
    True,
    electron,
    owner="signal-owner.cjs",
    extra=dict(
        signalPolicy=policy,
        researchDirectory=P.W.lp(P.BASE / "signal-site"),
    ),
)
assert result.get("observed"), result.get("failure")
events = result["kernelRows"]
signals = [row for row in events if row["event"] == "actual-posix-signal"]
assert signals and all(row["signum"] == 1 for row in signals)
closed = [
    row for row in events if row["event"] == "original-application-close-returned"
]
if policy == "observe-default":
    assert not closed
else:
    assert result["afterHostDeath"]["closeGateEntered"]
    assert result["afterHostDeath"]["kernel"]["alive"]
    assert not result["afterHostDeath"]["lock"]["acquired"]
    assert closed and result["pidfdObserver"]["rows"][-1]["signalled"]
assert result["beforeCleanup"]["postgres"]["exit"] == 0
print(
    json.dumps(
        dict(
            label=result["label"],
            policy=policy,
            actualSignals=signals,
            originalApplicationCloseReturned=bool(closed),
        )
    )
)
