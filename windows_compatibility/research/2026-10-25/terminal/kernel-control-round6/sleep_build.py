"""Pause a real ready owner for human sleep; resume the original Quit path."""

from pathlib import Path

BASE = Path(__file__).resolve().parent
source = (BASE / "owner.cjs").read_text(encoding="utf-8")
needle = "    if (config.scenario === 'host-loss') {"
assert source.count(needle) == 1
source = source.replace(
    needle,
    """    if (config.scenario === 'sleep') {
        assert(origin);
        emit('actual-kernel-sleep-ready');
        while (!fs.existsSync(config.sleepResumePath)) await new Promise(resolve => setTimeout(resolve, 100));
        emit('actual-kernel-sleep-resumed');
    }
"""
    + needle,
)
(BASE / "sleep-owner.cjs").write_text(source, encoding="utf-8")
