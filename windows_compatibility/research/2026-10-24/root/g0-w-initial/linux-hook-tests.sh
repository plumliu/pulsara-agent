set -eux
test "$(id -u)" = 1000
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/workspaces/pulsara_agent"
mise exec -- .venv/bin/python -m pytest -q tests/test_round9_2_hook_subsystem.py
