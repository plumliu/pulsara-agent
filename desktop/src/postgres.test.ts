import {test} from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp, mkdir, writeFile, rm, readFile} from 'node:fs/promises';
import path from 'node:path';
import {BundledPostgres} from './postgres';

test('failed readiness keeps a verified started server owned for explicit shutdown', async () => {
  // Keep the fixture inside PostgreSQL's Unix-socket pathname limit on macOS.
  const root = await mkdtemp('/tmp/pg-owner-');
  const pg = new BundledPostgres(root, root);
  try {
    await mkdir(path.join(root, 'postgres/bin'), {recursive:true});
    await mkdir(pg.directory, {recursive:true});
    await writeFile(path.join(pg.directory, 'PG_VERSION'), '17\n');
    await writeFile(path.join(root, 'postgres/bin/pg_ctl'), `#!/bin/sh
action="$1"; shift
while [ "$#" -gt 0 ]; do
  if [ "$1" = -D ]; then shift; data="$1"; fi
  shift
done
case "$action" in
  status) test -f "$data/postmaster.pid" || exit 3 ;;
  start) printf '123\\n%s\\n0\\n5432\\n%s\\n' "$data" "$(dirname "$data")/socket" > "$data/postmaster.pid"; exit 1 ;;
  stop) printf stopped > "$data/stop-result" ;;
esac
`, {mode:0o755});
    await assert.rejects(pg.start());
    assert.equal(pg.startedHere, true);
    await pg.stop();
    assert.equal(await readFile(path.join(pg.directory,'stop-result'),'utf8'), 'stopped');
  } finally { await rm(root, {recursive:true,force:true}); }
});
