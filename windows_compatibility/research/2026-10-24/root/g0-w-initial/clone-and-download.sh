set -eux
test "$(id -u)" = 1000
mkdir -p "$HOME/workspaces" "$HOME/.cache/pulsara-g0/20261024-environment"
test ! -e "$HOME/workspaces/pulsara_agent"
git -c core.autocrlf=false clone --no-hardlinks /mnt/c/python_workspace/pulsara-agent "$HOME/workspaces/pulsara_agent"
cd "$HOME/workspaces/pulsara_agent"
git config core.autocrlf false
git remote rename origin windows-source
git remote add origin https://github.com/plumliu/pulsara-agent.git
git rev-parse HEAD
git status --short --branch
findmnt -T . -o TARGET,SOURCE,FSTYPE,OPTIONS
curl -fsSL https://mise.run -o "$HOME/.cache/pulsara-g0/20261024-environment/mise-install.sh"
cat "$HOME/.cache/pulsara-g0/20261024-environment/mise-install.sh"
