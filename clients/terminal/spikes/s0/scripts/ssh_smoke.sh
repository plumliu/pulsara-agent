#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BINARY=${1:-"$ROOT/dist/packages/pulsara-tui-s0-linux-arm64"}
CONTAINER="pulsara-s0-sshd-$$"
TEMP=$(mktemp -d)
SSH_KEY="$TEMP/client_key"
STAGE=initializing
KNOWN_OPTIONS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=no
  -o UserKnownHostsFile=/dev/null
  -o LogLevel=ERROR
  -o ConnectTimeout=5
  -o ServerAliveInterval=1
  -o ServerAliveCountMax=3
)
DOCKER_HTTP_PROXY=${HTTP_PROXY:-${http_proxy:-}}
DOCKER_HTTPS_PROXY=${HTTPS_PROXY:-${https_proxy:-}}
DOCKER_HTTP_PROXY=${DOCKER_HTTP_PROXY//127.0.0.1/host.docker.internal}
DOCKER_HTTP_PROXY=${DOCKER_HTTP_PROXY//localhost/host.docker.internal}
DOCKER_HTTPS_PROXY=${DOCKER_HTTPS_PROXY//127.0.0.1/host.docker.internal}
DOCKER_HTTPS_PROXY=${DOCKER_HTTPS_PROXY//localhost/host.docker.internal}
PROXY_ARGS=()
if [[ -n "$DOCKER_HTTP_PROXY" ]]; then
  PROXY_ARGS+=( -e "HTTP_PROXY=$DOCKER_HTTP_PROXY" -e "http_proxy=$DOCKER_HTTP_PROXY" )
fi
if [[ -n "$DOCKER_HTTPS_PROXY" ]]; then
  PROXY_ARGS+=( -e "HTTPS_PROXY=$DOCKER_HTTPS_PROXY" -e "https_proxy=$DOCKER_HTTPS_PROXY" )
fi

cleanup() {
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$TEMP"
}
trap cleanup EXIT
trap 'echo "SSH smoke failed during: $STAGE" >&2; if [[ -n "${INTERACTIVE_OUTPUT:-}" && -f "$INTERACTIVE_OUTPUT" ]]; then strings "$INTERACTIVE_OUTPUT" | tail -n 40 >&2; fi; docker logs "$CONTAINER" >&2 2>/dev/null || true' ERR

if [[ ! -x "$BINARY" ]]; then
  echo "linux/arm64 binary is missing: $BINARY" >&2
  exit 2
fi

ssh-keygen -q -t ed25519 -N '' -f "$SSH_KEY"
STAGE=container_start
docker run -d \
  --name "$CONTAINER" \
  --cap-add NET_ADMIN \
  "${PROXY_ARGS[@]}" \
  -p 127.0.0.1::22 \
  -v "$BINARY:/usr/local/bin/pulsara-tui-s0:ro" \
  -v "$SSH_KEY.pub:/bootstrap/authorized_keys:ro" \
  alpine:3.22 \
  sh -ceu '
    apk add --no-cache openssh-server iproute2 >/dev/null
    ssh-keygen -A >/dev/null
    mkdir -p /run/sshd
    mkdir -p /root/.ssh
    cp /bootstrap/authorized_keys /root/.ssh/authorized_keys
    chmod 700 /root/.ssh
    chmod 600 /root/.ssh/authorized_keys
    printf "%s\n" \
      "PermitRootLogin prohibit-password" \
      "PasswordAuthentication no" \
      "KbdInteractiveAuthentication no" \
      "UsePAM no" \
      "AcceptEnv LANG LC_*" >> /etc/ssh/sshd_config
    tc qdisc add dev eth0 root netem delay 60ms 10ms || true
    exec /usr/sbin/sshd -D -e
  ' >/dev/null

if ! HOST_PORT=$(docker port "$CONTAINER" 22/tcp 2>/dev/null | awk -F: '{print $NF}'); then
  docker inspect -f '{{.State.Status}} exit={{.State.ExitCode}} error={{.State.Error}}' "$CONTAINER" >&2 || true
  docker logs "$CONTAINER" >&2 || true
  exit 1
fi
ready=false
STAGE=sshd_readiness
for _ in $(seq 1 300); do
  if ssh "${KNOWN_OPTIONS[@]}" -i "$SSH_KEY" -p "$HOST_PORT" root@127.0.0.1 true 2>/dev/null; then
    ready=true
    break
  fi
  if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
    break
  fi
  sleep 0.2
done
if [[ "$ready" != true ]]; then
  docker logs "$CONTAINER" >&2 || true
  echo "ephemeral SSH server did not become ready" >&2
  exit 1
fi
ssh "${KNOWN_OPTIONS[@]}" -i "$SSH_KEY" -p "$HOST_PORT" root@127.0.0.1 true

INTERACTIVE_OUTPUT="$TEMP/interactive.out"
STAGE=interactive_session
INTERACTIVE_FIFO="$TEMP/interactive.fifo"
mkfifo "$INTERACTIVE_FIFO"
exec 8<>"$INTERACTIVE_FIFO"
started_ns=$(python3 -c 'import time; print(time.time_ns())')
TERM=xterm-256color ssh -tt "${KNOWN_OPTIONS[@]}" -i "$SSH_KEY" -p "$HOST_PORT" root@127.0.0.1 \
  'stty cols 120 rows 32; exec env LC_CTYPE=UTF-8 /usr/local/bin/pulsara-tui-s0' <"$INTERACTIVE_FIFO" >"$INTERACTIVE_OUTPUT" 2>&1 &
INTERACTIVE_PID=$!
for _ in $(seq 1 200); do
  if grep -a -q 'Pulsara Bubble Tea S0' "$INTERACTIVE_OUTPUT"; then
    break
  fi
  sleep 0.05
done
grep -a -q 'Pulsara Bubble Tea S0' "$INTERACTIVE_OUTPUT"
printf 'SSH中文🙂ASCII' >&8
for _ in $(seq 1 100); do
  if grep -a -q 'SSH中文🙂ASCII' "$INTERACTIVE_OUTPUT"; then
    break
  fi
  sleep 0.05
done
printf '\021' >&8
wait "$INTERACTIVE_PID"
exec 8>&-
ended_ns=$(python3 -c 'import time; print(time.time_ns())')

STAGE=interactive_assertions
grep -a -q 'Pulsara Bubble Tea S0' "$INTERACTIVE_OUTPUT"
grep -a -q 'SSH中文🙂ASCII' "$INTERACTIVE_OUTPUT"
grep -a -q $'\033\[?1049h' "$INTERACTIVE_OUTPUT"
grep -a -q $'\033\[?1049l' "$INTERACTIVE_OUTPUT"

FIFO="$TEMP/disconnect.fifo"
DISCONNECT_OUTPUT="$TEMP/disconnect.out"
STAGE=disconnect_session
mkfifo "$FIFO"
exec 9<>"$FIFO"
TERM=xterm-256color ssh -tt "${KNOWN_OPTIONS[@]}" -i "$SSH_KEY" -p "$HOST_PORT" root@127.0.0.1 \
  'stty cols 120 rows 32; exec env LC_CTYPE=UTF-8 /usr/local/bin/pulsara-tui-s0' <"$FIFO" >"$DISCONNECT_OUTPUT" 2>&1 &
SSH_PID=$!
for _ in $(seq 1 100); do
  if grep -a -q 'Pulsara Bubble Tea S0' "$DISCONNECT_OUTPUT"; then
    break
  fi
  sleep 0.05
done
grep -a -q 'Pulsara Bubble Tea S0' "$DISCONNECT_OUTPUT"
kill -KILL "$SSH_PID"
wait "$SSH_PID" 2>/dev/null || true
exec 9>&-

RECONNECT_VERSION=$(ssh "${KNOWN_OPTIONS[@]}" -i "$SSH_KEY" -p "$HOST_PORT" root@127.0.0.1 \
  '/usr/local/bin/pulsara-tui-s0 --version')
STAGE=reconnect_assertion
[[ "$RECONNECT_VERSION" == pulsara-tui-s0* ]]

elapsed_ms=$(( (ended_ns - started_ns) / 1000000 ))
cat <<JSON
{
  "status": "pass",
  "transport": "OpenSSH over Docker loopback",
  "injected_server_egress_latency_ms": 60,
  "interactive_elapsed_ms": $elapsed_ms,
  "term": "xterm-256color",
  "locale": "LC_CTYPE=UTF-8",
  "cjk_input_visible": true,
  "alternate_screen_restored": true,
  "abrupt_disconnect_observed": true,
  "reconnect_version": "$RECONNECT_VERSION"
}
JSON
