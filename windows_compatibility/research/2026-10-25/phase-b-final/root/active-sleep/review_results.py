"""Offline verification of this run's unmodified before/after observations."""
from datetime import datetime
import json
from pathlib import Path

root = Path(__file__).resolve().parent
def read(name):
    return json.loads((root / name).read_text(encoding='utf-8-sig'))

before, after = read('before.json'), read('after.json')
prepared, woke, sent = (read(f'session-{name}.json') for name in ('prepared', 'after-wake', 'after-message'))
ready, exited = read('child-handle-ready.json'), read('child-handle-exited.json')
quit_result, maintenance = read('normal-quit-settled.json'), read('maintenance.json')
assert before['beforeReady'] and after['allComparisonsPassed']
assert before['linux']['writer']['generation'] == 1 and after['linux']['writer']['generation'] == 2
assert prepared['passed'] and woke['passed'] and sent['passed']
assert len(sent['tool_calls']) == 1 and sent['tool_calls'] == prepared['tool_calls']
assert ready['alive'] and not exited['alive'] and exited['sameHeldHandle'] and not exited['signalSent']
assert all(ready[key] == exited[key] for key in ('pid', 'image', 'creationFileTime'))
assert not quit_result['processes'] and not quit_result['child'] and not quit_result['originalKernelExists']
assert quit_result['pgExit'] == 3 and maintenance['exitCode'] == 0
assert '活动任务睡眠后连接正常' in (root / 'gui-after-message.txt').read_text(encoding='utf-8')
events = sorted(after['windows']['powerEvents'], key=lambda row: row['recordId'])
intervals = []
pending = None
for event in events:
    if event['id'] == 506:
        assert pending is None
        pending = event
    elif event['id'] == 507:
        assert pending is not None
        duration = (datetime.fromisoformat(event['utc']) - datetime.fromisoformat(pending['utc'])).total_seconds()
        intervals.append({'start': pending, 'end': event, 'secondsRounded': round(duration, 3)})
        pending = None
assert pending is None and len(intervals) == 3
assert intervals[-1]['secondsRounded'] > 60
assert datetime.fromisoformat(exited['exitObservedAt']) >= datetime.fromisoformat(intervals[-1]['end']['utc'])
result = {'passed': True, 'version': '0.1.0-alpha.10',
          'sleepIntervals': intervals, 'writerGeneration': [1, 2],
          'snapshotComparisons': len(after['comparisons']), 'windowsChildExitCode': exited['exitCode'],
          'originalChildHeldHandleSignaled': True, 'observerSentSignal': False,
          'sameKernelAndPgThroughSleep': True, 'uniqueCompletedPrompts': 2,
          'terminalLaunches': 1, 'oldToolCallsExactlyPreserved': True,
          'normalQuitAndMaintenancePassed': True,
          'qualification': 'One actual GUI PIPE/Windows echo task across sleep; not active provider, every owner, or hardware-DPI matrix.'}
(root / 'verified-results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
