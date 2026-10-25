"""The deterministic observation regression against Git's original output owner."""

from pathlib import Path

source = Path(__file__).with_name("verify_owners.py").read_text()
source = source.replace("for phase in ('baseline','candidate'):", "for phase in ('candidate',):")
source = source.replace("if phase=='baseline':", "if True:")
source = source.replace("HEAD:src/pulsara_agent/terminal_process/manager.py", "HEAD:src/pulsara_agent/terminal_process/output.py")
source = source.replace("(snapshot/'src/pulsara_agent/terminal_process/manager.py').write_bytes(original)", "(snapshot/'src/pulsara_agent/terminal_process/output.py').write_bytes(original)")
source = source.replace("tests=['tests/test_terminal_startup_cleanup.py']", "tests=['tests/test_terminal_output_settlement.py']")
source = source.replace("if phase=='candidate': tests+=", "if False: tests+=")
source = source.replace('"--candidate-only" not in sys.argv', 'False')
source = source.replace('"posix-results.json"', '"output-observation-baseline.json"')
source = source.replace('assert result["results"][-1]["exit"] == 0', 'assert result["results"][-1]["exit"] != 0')
exec(compile(source, __file__, "exec"))
