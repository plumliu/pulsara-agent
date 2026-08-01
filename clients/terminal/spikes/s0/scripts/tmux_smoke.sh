#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
BINARY=${1:-"$ROOT/dist/pulsara-tui-s0"}
SESSION="pulsara-s0-$$"
TARGET="$SESSION:0.0"
RAW_OUTPUT=$(mktemp)
PASTE_PAYLOAD=$(mktemp)

cleanup() {
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  rm -f "$RAW_OUTPUT" "$PASTE_PAYLOAD"
}
trap cleanup EXIT

if [[ ! -x "$BINARY" ]]; then
  echo "binary is not executable: $BINARY" >&2
  exit 2
fi

wait_until() {
  local description=$1
  local command=$2
  local deadline=$((SECONDS + 10))
  until eval "$command"; do
    if (( SECONDS >= deadline )); then
      echo "timeout waiting for: $description" >&2
      tmux capture-pane -p -t "$TARGET" -S -100 >&2 || true
      exit 1
    fi
    sleep 0.05
  done
}

tmux new-session -d -s "$SESSION" -x 120 -y 32 -c "$ROOT"
tmux set-option -t "$SESSION" remain-on-exit on
tmux pipe-pane -t "$TARGET" "exec cat > '$RAW_OUTPUT'"
tmux send-keys -l -t "$TARGET" "$BINARY"
tmux send-keys -t "$TARGET" Enter

wait_until "Bubble Tea alternate screen" "[[ \$(tmux display-message -p -t '$TARGET' '#{alternate_on}') == 1 ]]"
wait_until "S0 first frame" "tmux capture-pane -p -t '$TARGET' | grep -q 'Pulsara Bubble Tea S0'"

tmux send-keys -l -t "$TARGET" "tmux中文🙂ASCII"
for size in "80 24" "120 32" "160 40" "12 4" "120 32"; do
  read -r width height <<<"$size"
  tmux resize-window -t "$SESSION" -x "$width" -y "$height"
  wait_until "resize ${width}x${height}" "tmux capture-pane -p -t '$TARGET' | grep -q 'size=${width}x${height}'"
done

LC_ALL=C tr '\0' x < /dev/zero | head -c 1048576 > "$PASTE_PAYLOAD" || true
tmux load-buffer -b pulsara-s0-paste "$PASTE_PAYLOAD"
tmux paste-buffer -p -b pulsara-s0-paste -t "$TARGET"
wait_until "1 MiB bracketed paste externalization" "tmux capture-pane -p -t '$TARGET' | grep -q 'large paste: 1048576 bytes'"

tmux send-keys -t "$TARGET" C-q
wait_until "alternate screen restore" "[[ \$(tmux display-message -p -t '$TARGET' '#{alternate_on}') == 0 ]]"

grep -q $'\033\[?1049h' "$RAW_OUTPUT"
grep -q $'\033\[?1049l' "$RAW_OUTPUT"
grep -q $'\033\[?2004h' "$RAW_OUTPUT"
grep -q $'\033\[?2004l' "$RAW_OUTPUT"
grep -q $'\033\[?1002h' "$RAW_OUTPUT"
grep -q $'\033\[?1002l' "$RAW_OUTPUT"
grep -q $'\033\[?1006h' "$RAW_OUTPUT"
grep -q $'\033\[?1006l' "$RAW_OUTPUT"

cat <<JSON
{
  "status": "pass",
  "tmux_version": "$(tmux -V)",
  "alternate_screen_entered": true,
  "alternate_screen_restored": true,
  "bracketed_paste_entered": true,
  "bracketed_paste_restored": true,
  "mouse_cell_motion_entered": true,
  "mouse_cell_motion_restored": true,
  "large_paste_bytes": 1048576,
  "resize_matrix": ["80x24", "120x32", "160x40", "12x4"]
}
JSON
