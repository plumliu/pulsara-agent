# Bubble Tea v2 S0 feasibility spike

This directory is a disposable, non-production probe for `TUI-BT-S0-*`.
It does not connect to Pulsara Runtime, EventLog, the production terminal
protocol, or real secret material.

Pinned inputs:

- `charm.land/bubbletea/v2 v2.0.6`
- `charm.land/bubbles/v2 v2.1.0`
- `charm.land/lipgloss/v2 v2.0.5`
- `google.golang.org/protobuf v1.36.11`

The Python parent and Go child exchange only the fake, length-prefixed
Protobuf `Snapshot` and `Delta` messages in `probe_wire/probe.proto` over an
inherited file descriptor. The Bubble Tea program alone owns its PTY.

## Automated smoke

From this directory:

```bash
scripts/run_automated.sh
```

The command exercises:

- CJK, emoji, wide-rune cursor insertion/deletion, multiline textarea,
  Shift+Enter handling and Ctrl+J fallback;
- a bounded client-owned undo/redo wrapper (Bubbles textarea has no native
  undo stack);
- small, cancelled, and 1 MiB bracketed-paste paths;
- 20Hz and 100Hz fake Protobuf streams while the draft remains editable;
- 80/120/160-column and extreme resize paths;
- tmux alternate-screen, bracketed-paste, resize, and terminal-mode restore;
- normal quit, panic, SIGTERM, SIGINT, and SIGKILL parent recovery;
- darwin/linux amd64/arm64 cross-builds, checksums, module pins, and native
  launch;
- an actual OpenSSH remote PTY in an isolated Linux container, including CJK,
  injected latency, abrupt disconnect, and reconnect.

Generated evidence is written to `evidence/`. Built binaries remain under the
ignored `dist/` directory.

## Repeatable resource/render baseline

Run the frozen 20-repetition matrix separately from the fast smoke suite:

```bash
scripts/run_performance.sh
```

For each 20Hz and 100Hz workload, the benchmark uses a 1-second warm-up,
3-second measured active window, 10Hz process sampling, and 20 interleaved CJK
keypress probes. It records nearest-rank p50/p95/p99, CPU average/peak,
resident-memory steady/peak/growth, scheduling lag, delivery latency, and the
interval between Bubble Tea's physical non-empty PTY output writes. Results and
all per-run summaries are written to:

- `evidence/darwin-arm64-performance.json`
- `evidence/darwin-arm64-performance.md`

On macOS the resource sampler calls `proc_pid_rusage(RUSAGE_INFO_V2)` directly;
on Linux it reads `/proc/<pid>/stat` and `statm`. The benchmark has no `psutil`
or production Pulsara dependency. The frozen thresholds are feasibility guards,
not product SLOs.

To regenerate the fake wire bindings from the pinned schema/compiler plugin:

```bash
scripts/generate_proto.sh
```

## Required manual observations

Automation sends already-composed UTF-8; it cannot prove a macOS input
method's pre-edit/candidate lifecycle. Before S0 can be marked PASS, run:

```bash
go run ./cmd/pulsara-tui-s0
```

Record the terminal name/version and verify all of the following in a real
interactive terminal:

1. Use macOS Pinyin (or another real IME) to enter `中文输入，。！？` and commit
   multiple candidates. No draft character may disappear or trigger a false
   submit.
2. Append `mixed-ASCII🙂👨‍👩‍👧‍👦`, then move Home/End/Left/Right across every
   wide grapheme and edit on both sides.
3. Verify Shift+Enter if `keyboard=true` is reported. If the terminal does not
   support key disambiguation, verify Ctrl+J as the explicit newline fallback.
4. Paste multiline text and a 1 MiB payload. The latter must report
   `large paste: 1048576 bytes` and must not become textarea content.
5. Repeat inside a real attached tmux client, not only the detached automated
   probe.
6. Repeat over a real remote SSH host with the intended locale/TERM and record
   any terminal-emulator/IME-specific visual behavior. The automated real-host
   network/PTTY/disconnect/reconnect record is complete; this remaining manual
   pass is only for visual IME behavior.

Use Ctrl+Q for normal exit and Ctrl+G for the intentional panic probe. The S0
feasibility decision is **PASS**. Real IME, attached tmux visual checks, remote
terminal visual checks, and non-native clean-runner launch records remain
deferred compatibility/release regressions and do not block S1. The repeated
CPU/RSS/render-cadence and real-host SSH automation are complete.

## Real-host SSH smoke

The target used for the current evidence is a Windows OpenSSH host with WSL2.
The probe deliberately uploads the existing `linux/amd64` package into WSL; it
does not add Windows to the production target matrix:

```bash
scripts/real_ssh_smoke.sh \
  plumlocal@plumliuwin.local \
  dist/packages/pulsara-tui-s0-linux-amd64 \
  evidence/real-ssh-plumliuwin-wsl2-amd64.json
```

The probe checks the uploaded SHA-256, UTF-8/CJK input, TERM, PTY raw/restore,
alternate screen, bracketed paste, 20 keypress latency samples, abrupt local
SSH death, remote-process exit, reconnect, and exact removal of both Windows
and WSL staging files. The real-network gate is first frame at most 3 seconds,
keypress p95 at most 150ms, and p99 at most 250ms; it intentionally does not
reuse the local PTY's tighter threshold.
