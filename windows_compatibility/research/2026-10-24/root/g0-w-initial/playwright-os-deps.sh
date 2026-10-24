set -eux
test "$(id -u)" = 0
/home/plumwsl/workspaces/pulsara_agent/.venv/bin/python -m playwright install-deps chromium
