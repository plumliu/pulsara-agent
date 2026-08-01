# Bubble Tea S0 CPU / RSS / renderer cadence baseline

> generated: `2026-08-01T10:55:25.253544+00:00`
> overall gate: **PASS**
> platform: `macOS-26.2-arm64-arm-64bit`

## Frozen measurement contract

- repetitions: 20 per workload
- warm-up: 1.0s
- measured active window: 3.0s
- resource sampling: 10.0Hz
- key probes: 20 per run
- percentile algorithm: nearest-rank over raw samples; workload summaries are nearest-rank over per-run statistics
- renderer cadence: physical non-empty writes at Bubble Tea's output writer

## Results

| workload | gate | key p95 | delivery p95 | render interval p99 | render jitter | CPU avg | RSS peak |
|---:|:---:|---:|---:|---:|---:|---:|---:|
| 20hz | pass | 20.227ms | 0.429ms | 87.701ms | 16.759ms | 4.500% | 15.578MiB |
| 100hz | pass | 20.418ms | 0.762ms | 45.035ms | 1.866ms | 10.039% | 16.828MiB |

## Frozen workload gates

| workload | key p95/p99 | delivery p95 | render p99/jitter | CPU avg/peak | RSS peak/growth |
|---:|---:|---:|---:|---:|---:|
| 20hz | ≤50/100ms | ≤10ms | ≤100/50ms | ≤25/150% | ≤128/16MiB |
| 100hz | ≤50/100ms | ≤10ms | ≤100/50ms | ≤50/150% | ≤128/16MiB |

Cross-run allowed p95−p05 spread: keypress p95 ≤25ms, CPU average ≤20 percentage points, RSS steady p95 ≤16MiB, renderer interval p95 ≤25ms.

## Gate details

- `20hz`: all checks passed
- `100hz`: all checks passed

CPU peak and cross-run spread are host-sensitive feasibility guards, not product SLOs. The JSON evidence retains every per-run summary.
