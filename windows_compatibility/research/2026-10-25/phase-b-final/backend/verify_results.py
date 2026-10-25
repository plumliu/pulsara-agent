"""Offline checks of raw owner calls, lock facts and actual terminal outcomes."""
import json
from pathlib import Path

root = Path(__file__).resolve().parent
names = ('pre-cancel', 'start-loss', 'stop-loss', 'init-loss', 'readiness')
results = {name: json.loads((root / name / 'result.json').read_text()) for name in names}
checks = {}
reset = ['resetPathsMatch', 'dataDirectoryInfo', 'readVersion', 'run:pg_ctl:status', 'removeDataDirectory']
for name, value in results.items():
    assert value['passed'] is True
    assert value['provenance']['node'] == 'v22.23.3'
    assert all(value['provenance'][key] for key in ('installedTsExactCurrentDist', 'productionOwner', 'productionClient', 'productionWslHost', 'installedHelperWithTestGates'))
    calls = value['cleanup']['calls']
    assert all(row['closed']['signal'] is None for row in calls)
    target = next(row for row in calls if row['armed'])
    assert sum(row['armed'] for row in calls) == 1
    after = calls[calls.index(target) + 1:]
    methods = [row['method'] for row in after]
    final = value['cleanup']['facts']
    assert final['data_exists'] is False and final['outside'] == 'outside-sentinel'
    assert all(not final['locks'][lock]['busy'] for lock in ('kernel.lock', 'pg-lifetime.lock'))
    assert value['cleanup']['owner'] == {'startedHere': False, 'initialized': False}
    if name == 'pre-cancel':
        assert target['operationBytes'] == 0 and target['closed']['code'] == 0
        assert value['outcome']['error']['execution'] == 'not-submitted'
        assert value['outcome']['owner']['startedHere'] is False
        assert value['foreignOwner']['startedHere'] is True
        assert value['outcome']['facts']['status']['exit'] == 0
        assert methods == ['run:pg_ctl:stop', *reset]
    elif name in ('start-loss', 'stop-loss', 'init-loss'):
        assert target['operationBytes'] > 0 and target['closed']['code'] == 9
        assert value['outcome']['error']['execution'] == 'unknown'
        refusal = value['inflightRefusals']
        expected_code = 'PG_LIFETIME_BUSY' if name == 'start-loss' else 'PG_KERNEL_BUSY'
        assert all(error['execution'] == 'not-submitted' and error['code'] == expected_code for error in refusal['competition'].values())
        facts = refusal['facts']
        assert facts['locks']['kernel.lock']['busy'] == (name != 'start-loss')
        assert facts['locks']['pg-lifetime.lock']['busy'] == (name != 'stop-loss')
        assert refusal['untouched']['outside'] == 'outside-sentinel'
        assert value['outcome']['owner']['startedHere'] == (name == 'stop-loss')
        if name == 'start-loss':
            assert facts['status']['exit'] == 3
            assert value['lateQuery']['identity'] == value['initialQuery']['identity']
            assert methods == ['prepareLayout', 'readVersion', 'run:pg_ctl:status', 'socketMode', 'run:pg_ctl:start', *reset, 'maintenanceCheck', 'run:pg_ctl:stop', *reset]
        elif name == 'stop-loss':
            assert facts['status']['exit'] == 3
            assert methods == ['prepareLayout', 'resetPathsMatch', 'maintenanceCheck', *reset]
        else:
            assert value['outcome']['owner']['initialized'] is False
            assert methods == ['prepareLayout', 'resetPathsMatch', 'maintenanceCheck', 'maintenanceCheck']
            assert value['emptyLayoutMaintenance']['passed']
    else:
        assert target['operationBytes'] > 0 and target['closed']['code'] == 0
        assert value['outcome']['error']['execution'] == 'failed' and value['outcome']['error']['code'] == 1
        assert 'server did not start in time' in value['outcome']['error']['stderr']
        assert value['outcome']['owner']['startedHere'] is True
        assert value['readyBeforeFailureReply']['identity'] == value['initialQuery']['identity']
        assert methods == ['run:pg_ctl:status', 'readInstance', 'run:pg_ctl:stop', *reset]
    checks[name] = {'passed': True, 'original_call_and_explicit_followups_exact': True,
                    'all_carriers_closed': True, 'clean_data_and_locks': True}
cleanup = json.loads((root / 'cleanup.json').read_text())
assert cleanup['all_removed'] and not cleanup['remaining_accessible_exact_owned'] and not cleanup['known_actor_same_identity_alive']
assert len(cleanup['snapshots']) == 6
assert json.loads((root / 'safe-checkpoint.json').read_text())['safe_for_sleep']
assert 'failure' in json.loads((root / 'first-pre-cancel/result.json').read_text())
assert json.loads((root / 'first-init-loss/result.json').read_text())['failure']['code'] == 'ENOENT'
output = {'passed': True, 'case_count': len(checks), 'checks': checks,
          'original_fixture_failures_preserved': 2, 'production_changed': False,
          'cleanup_access_qualification': cleanup['qualification'],
          'same_uid_proc_inaccessible': cleanup['same_uid_proc_inaccessible']}
(root / 'verified-results.json').write_text(json.dumps(output, indent=2), encoding='utf-8')
print(json.dumps(output))
