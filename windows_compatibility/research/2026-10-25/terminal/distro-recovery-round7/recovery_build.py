"""Only add observation/decision gates to the same actual Runtime/PG owners."""

from pathlib import Path

BASE = Path(__file__).resolve().parent
source = (BASE / "distro-owner.cjs").read_text(encoding="utf-8")
needle = "        await postgres.start();"
assert source.count(needle) == 1
source = source.replace(
    needle,
    "        await runtime.waitForPreviousOwner();\n        emit('original-wait-before-pg-start-returned');\n"
    + needle,
)
needle = "    }, true);"
assert source.count(needle) == 1
source = source.replace(needle, "    }, config.initialize);")
begin = source.index("    if (config.scenario === 'distro-loss') {")
end = source.index("    if (config.scenario === 'host-loss') {", begin)
source = (
    source[:begin]
    + """    if (config.scenario === 'distro-loss') {
        assert(origin);
        emit('temporary-distro-terminate-ready');
        await until(() => runtime.child === undefined, 120);
        emit('temporary-distro-carrier-closed-before-restart', {runtimeRunning: runtime.running});
        fs.writeFileSync(config.resultPath, JSON.stringify({rows, appExit: null, pgStopCalls, remoteOutcome: 'unknown'}, null, 2));
        return;
    }
    if (config.scenario === 'recovery-after') {
        assert(origin);
        emit('recovery-new-attempt-ready');
        await until(() => fs.existsSync(config.finishPath), 120);
    }
"""
    + source[end:]
)
(BASE / "recovery-owner.cjs").write_text(source, encoding="utf-8")
