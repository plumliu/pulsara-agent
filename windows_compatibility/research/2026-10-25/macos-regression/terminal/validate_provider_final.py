from pathlib import Path
import json
import subprocess
import psycopg

OUT = Path(__file__).resolve().parent
report = json.loads((OUT / 'provider-final-results.json').read_text())
assert report['status'] == 'completed' and report['database_cleanup']
assert [item['case'] for item in report['cases']] == ['directories', 'interaction', 'pty', 'monitor_wake']
records = report['provider_records']
first = records[0]['provider_input']
serialize = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
system = serialize([m for m in first['messages'] if m['role'] == 'system'])
tools = serialize(first['tools'])
previous = []
for record in records:
    value = record['provider_input']
    assert serialize([m for m in value['messages'] if m['role'] == 'system']) == system
    assert serialize(value['tools']) == tools
    assert value['messages'][:len(previous)] == previous
    previous = value['messages']
trace = report['trace']
assert all(item['result_state'] == 'SUCCESS' for item in trace)
assert [item['result']['output'].strip() for item in trace[:3]] == ['alpha', 'beta', 'alpha']
workspace = Path(trace[4]['result']['cwd'])
assert 'COGNITIVE_FLAG=inside' in trace[3]['result']['output']
assert trace[4]['result']['output'] == f'pwd={workspace}\nCOGNITIVE_FLAG=\n'
assert 'export' not in trace[4]['arguments']['command']
assert [Path(item['result']['cwd']) for item in trace[:3]] == [workspace / 'alpha', workspace / 'beta', workspace / 'alpha']
assert all(item['result']['cwd'] == str(workspace) for item in trace[3:5])
starts = [item for item in trace if item['tool_name'] == 'terminal' and 'repl.py' in item['arguments'].get('command', '')]
assert len(starts) == 2
pipe = next(item for item in starts if not item['arguments'].get('tty'))
pty = next(item for item in starts if item['arguments'].get('tty'))
pipe_id = pipe['result']['process_id']
pty_id = pty['result']['process_id']
pipe_io = [item for item in trace if item['tool_name'] == 'terminal_process' and item['arguments']['process_id'] == pipe_id]
assert any(item['arguments'].get('action') == 'write' and item['arguments'].get('data') == 'hello' and 'ANSWER:helloworld' not in item['result']['output'] for item in pipe_io)
assert any(item['arguments'].get('action') == 'submit' and item['arguments'].get('data') == 'world' and 'ANSWER:helloworld' in item['result']['output'] for item in pipe_io)
assert any(item['arguments'].get('data') == 'quit' and item['result']['exit_code'] == 0 for item in pipe_io)
pty_io = [item for item in trace if item['tool_name'] == 'terminal_process' and item['arguments']['process_id'] == pty_id]
assert any(item['arguments'].get('data') == 'delayed' and 'ANSWER:delayed' in item['result']['output'] for item in pty_io)
assert any(item['arguments'].get('data') == 'quit' and item['result']['exit_code'] == 0 for item in pty_io)
monitor_start = next(item for item in trace if item['tool_name'] == 'terminal' and 'MONITOR_WAKE_DONE' in item['arguments'].get('command', ''))
monitor_id = monitor_start['result']['process_id']
assert any(item['tool_name'] == 'terminal_monitor' and item['arguments']['action'] == 'register' and item['arguments']['process_id'] == monitor_id for item in trace)
wake_case = report['cases'][-1]
wake = [json.loads(item['content']) for item in wake_case['wake_transcript'] if item['entry_kind'] == 'TERMINAL_OBSERVATION']
assert len(wake) == 1 and wake[0]['process_id'] == monitor_id
assert wake[0]['output'] == 'MONITOR_WAKE_DONE' and wake[0]['exit_code'] == 0
assert any('MONITOR_WAKE_DONE' in (item.get('assistant_text') or '') for item in wake_case['wake_transcript'])
private = json.loads((OUT.parents[0] / 'root/private-pg.json').read_text())
with psycopg.connect(private['admin_dsn'], autocommit=True) as connection:
    assert connection.execute('SELECT datname FROM pg_database WHERE datname=%s', (report['database']['name'],)).fetchall() == []
assert not workspace.parent.exists()
owned = [line for line in subprocess.run(['ps', '-axo', 'pid,args'], capture_output=True, text=True, check=True).stdout.splitlines() if str(workspace) in line]
assert owned == []
result = {'status': 'passed', 'provider_calls': len(records), 'tools_per_call': len(first['tools']), 'tool_call_count': len(trace), 'system_identical': True, 'provider_tools_identical': True, 'messages_append_only': True, 'observed_directory_sequence': ['alpha','beta','alpha'], 'separate_command_environment_value': '', 'pipe_answer': 'ANSWER:helloworld', 'pty_answer': 'ANSWER:delayed', 'monitor_autonomous_wake_seconds': wake_case['wake_elapsed_seconds'], 'monitor_exact_process_id': monitor_id, 'monitor_exit': 0, 'database_dropped': True, 'temporary_home_workspace_removed': True, 'owned_repl_processes': owned}
(OUT / 'provider-final-validation.json').write_text(json.dumps(result, indent=2) + '\n')
print(json.dumps(result, indent=2))
