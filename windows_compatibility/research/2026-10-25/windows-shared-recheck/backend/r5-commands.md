# R5 pending execution

Only after root confirms the new installed alpha10 workbench and requests the
after snapshot, run the first command. The original before file is preserved.
Then run context with a newly created private cluster; no guessed old socket or
development helper is accepted. These commands have **not** been executed yet.

```text
wsl.exe -d Ubuntu -u plumwsl --cd /home/plumwsl --exec /usr/bin/env PULSARA_HOME=/home/plumwsl/.pulsara /home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/python/bin/python3 -I /mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/backend/observe_upgrade.py after --version 0.1.0-alpha.10
```

```text
wsl.exe -d Ubuntu -u plumwsl --cd /home/plumwsl/workspaces/pulsara_agent --exec /home/plumwsl/workspaces/pulsara_agent/.venv/bin/python /mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/backend/run_r5.py --repo /home/plumwsl/workspaces/pulsara_agent --installed-root /home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10 --connection-id connection:oeeiipbn63ytbexaxw74nv5tgy --model deepseek-flash --output /mnt/c/python_workspace/pulsara-agent/output/windows-shared-recheck/backend/r5-first
```

The outer test harness uses the existing project environment for private PG
orchestration. The actual provider child always uses the new installed Python
with `-I`; it verifies product imports and the three complete source files before
adding tests/helpers. The original saved settings owner revalidates the selected
connection. Private data/socket/system identifier are constructed and read back
in this invocation. Standard PostgreSQL CLI creates this test cluster, and the
existing dogfood factory owns its UUID database. The formal Windows installed
helper, actual configure_interop target/Python, and private native-ext4 homes are
recorded in `context/r5-provenance.json`. Setup uncertainty preserves the exact
private root for inspection; no unconfirmed start permits recursive cleanup.
