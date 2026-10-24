"""Observe real SIGHUP, then test only an inherited startup disposition seam."""

from pathlib import Path

BASE = Path(__file__).resolve().parent
owner = (BASE / "owner.cjs").read_text(encoding="utf-8")
needle = "'G0_KERNEL_MODE=' + config.scenario,"
assert owner.count(needle) == 1
owner = owner.replace(needle, needle + " 'G0_SIGNAL_POLICY=' + config.signalPolicy,")
(BASE / "signal-owner.cjs").write_text(owner, encoding="utf-8")
site = BASE / "signal-site"
site.mkdir(exist_ok=True)
source = (BASE / "sitecustomize.py").read_text(encoding="utf-8")
source = source.replace("import os\n", "import os\nimport signal\n")
needle = '    log("kernel-process-started", home=str(home))'
assert source.count(needle) == 1
source = source.replace(
    needle,
    """    log("kernel-process-started", home=str(home))
    policy = os.environ["G0_SIGNAL_POLICY"]
    assert policy in {"observe-default", "ignore-hup"}

    def observed_hup(signum, _frame):
        log("actual-posix-signal", signum=signum, signalName=signal.Signals(signum).name, policy=policy)
        if policy == "observe-default":
            signal.signal(signum, signal.SIG_DFL)
            signal.raise_signal(signum)

    signal.signal(signal.SIGHUP, observed_hup)
    log("research-startup-signal-policy-installed", policy=policy)""",
)
source = source.replace("threading.Lock()", "threading.RLock()")
(site / "sitecustomize.py").write_text(source, encoding="utf-8")
