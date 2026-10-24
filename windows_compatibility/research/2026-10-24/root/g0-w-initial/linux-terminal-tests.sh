set -eux
test "$(id -u)" = 1000
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/workspaces/pulsara_agent"
mise exec -- .venv/bin/python -m pytest -q tests/test_round2_terminal_environment.py tests/test_round2_terminal_output.py tests/test_pr03_process_termination.py tests/test_stage2_terminal_host_lifetime.py tests/test_terminal_cognitive_subtraction.py tests/test_mcp_transport_lifecycle.py tests/test_mcp_sdk_transport_probe.py
