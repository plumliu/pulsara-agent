set -eux
test "$(id -u)" = 1000
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/workspaces/pulsara_agent"
mise run --jobs 1 desktop:verify
