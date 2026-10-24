# 小证据清单

- `report.zh.md`、本清单、`environment.json`。
- `job_probe.py`、`job_scope_checks.py`、`job_anchor.py`、`anchor_smoke.py`、`verify_cleanup.py`、`node_owner.cjs`、`electron_owner.cjs`。
- `matrix-summary.json`、`matrix-repeat.log`；首次错误预期的原始 `matrix.log`、`carrier-cancel-90a88fd7.jsonl`。
- `scope-summary.json`、`scope-exact-owner.log`，以及 summary列出的8个 `*.jsonl`、相同名字 `.jsonl.linux.jsonl`，Node/Electron两个 `.jsonl.host-output`。
- `anchor-summary.json`、`anchor-smoke.log`，其索引列出的3个record、相应 `.ready.json`。
- `cleanup-final.json`、`cleanup-final.log`。

不归档 Electron profile、`__pycache__` 或系统/应用二进制；不增加内容hash。全部研究输出仍在Git忽略的output范围，未提交。
