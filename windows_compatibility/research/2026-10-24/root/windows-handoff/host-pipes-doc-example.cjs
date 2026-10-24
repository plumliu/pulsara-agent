const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const path = require('node:path');
const env = {...process.env};
// Windows环境键名不区分大小写，复制成普通JS对象后要逐键清除。
for (const key of Object.keys(env)) {
  if (key.toUpperCase() === 'WSLENV') delete env[key];
}
const repo = '/home/pulsara/workspaces/pulsara_agent';
const code = 'import sys; data=sys.stdin.buffer.read(); sys.stderr.buffer.write(b"x"*(2*1024*1024)); sys.stderr.buffer.flush(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); sys.exit(7)';
const child = spawn(path.join(process.env.SystemRoot, 'System32', 'wsl.exe'),
  ['-d', 'Pulsara', '-u', 'pulsara', '--cd', repo, '--exec',
   repo + '/.venv/bin/python', '-u', '-c', code],
  {shell:false, stdio:['pipe','pipe','pipe'], env});
const payload = Buffer.concat([Buffer.from('中文 空格\r\n'), Buffer.alloc(1024*1024, 0x61)]);
const output = [];
let stderrBytes = 0;
child.stdout.on('data', chunk => output.push(chunk));
child.stderr.on('data', chunk => {stderrBytes += chunk.length;});
child.on('error', error => {console.error(error); process.exitCode = 1;});
child.stdin.on('error', error => {console.error(error); process.exitCode = 1;});
child.on('close', (code, signal) => {
  try {
    assert.equal(code, 7); assert.equal(signal, null);
    assert.deepEqual(Buffer.concat(output), payload);
    assert.equal(stderrBytes, 2*1024*1024);
    console.log('PASS basic pipe bytes / EOF / stderr drain / exit7');
  } catch (error) {console.error(error); process.exitCode = 1;}
});
child.stdin.write(payload.subarray(0, 1)); // 刻意从UTF-8字符中间分段。
child.stdin.end(payload.subarray(1));
