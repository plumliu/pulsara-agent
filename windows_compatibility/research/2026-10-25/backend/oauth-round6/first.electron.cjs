// OAuth-only host sink. Authorization URL stays private and is never logged.
const {app, shell} = require('electron');
const {spawn} = require('node:child_process');
const http = require('node:http');
const path = require('node:path');
const fs = require('node:fs');

const mode = process.argv[2] || 'mock';
if (!['mock', 'real'].includes(mode)) throw new Error('invalid research mode');
if (mode === 'real' && !process.argv.includes('--approved')) throw new Error('root/user readiness required');
app.setPath('userData', path.join(__dirname, 'electron-profile'));
const records = [];
let child, closing = false, current = {name: '', live: false};
const work = new Set();
function emit(event, facts = {}) {
  const row = {event, time: Date.now(), ...facts}; records.push(row);
  process.stdout.write(JSON.stringify(row) + '\n');
}
function send(value) {
  if (!child || child.stdin.destroyed || child.stdin.writableEnded) return false;
  child.stdin.write(JSON.stringify(value) + '\n'); return true;
}
function validate(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).sort().join(',') !== 'requestId,type,url' || value.type !== 'open-oauth-browser' || typeof value.requestId !== 'string' || typeof value.url !== 'string') throw new Error('invalid OAuth request');
  const url = new URL(value.url);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.hash) throw new Error('invalid OAuth URL');
  if (url.protocol === 'http:' && !['127.0.0.1', 'localhost', '[::1]'].includes(url.hostname)) throw new Error('non-loopback OAuth HTTP URL');
  return url;
}
function head(origin) {
  return new Promise(resolve => {
    const url = new URL('/__g0_oauth6_presence__', origin);
    const request = http.request(url, {method: 'HEAD'}, response => {response.resume(); resolve({reachable: true, status: response.statusCode});});
    request.once('error', error => resolve({reachable: false, errorCode: error.code}));
    request.setTimeout(3000, () => request.destroy()); request.end();
  });
}
async function openRequest(frame, flow) {
  let opened = false;
  try {
    const url = validate(frame);
    const redirect = url.searchParams.get('redirect_uri');
    if (!redirect) throw new Error('missing callback URL');
    const callback = new URL(redirect);
    if (callback.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(callback.hostname) || callback.search || callback.hash || callback.username) throw new Error('callback URL not loopback');
    const presence = await head(callback.origin);
    emit('listener-present-before-open', {requestId: frame.requestId, case: flow.name, ...presence});
    if (!presence.reachable || closing || !flow.live) throw new Error('OAuth request retired');
    // No asynchronous gap between the last known retirement check and the OS sink.
    const opening = shell.openExternal(frame.url);
    emit('shell-open-external-submitted', {requestId: frame.requestId, case: flow.name, origin: url.origin});
    await opening;
    opened = true;
    emit('shell-open-external-returned', {requestId: frame.requestId, case: flow.name, opened: true});
  } catch {
    emit('oauth-open-refused-or-failed', {requestId: typeof frame?.requestId === 'string' ? frame.requestId : '', case: flow.name});
  }
  send({type: 'open-oauth-browser-result', requestId: frame.requestId, opened});
}
function receive(frame) {
  if (frame.type === 'open-oauth-browser') {
    const task = openRequest(frame, current).finally(() => work.delete(task)); work.add(task); return;
  }
  // Guest diagnostic events have no URL, callback code, token or client secret.
  if (frame.type === 'case-start') current = {name: frame.case, live: true};
  if (frame.type === 'mock-authorization-visited' && frame.case === 'cancel') send({type: 'cancel-login'});
  if (frame.type === 'mock-authorization-visited' && frame.case === 'pipe-loss') {
    current.live = false;
    child.stdin.end(); emit('host-control-pipe-eof', {case: frame.case});
  }
  if (frame.type === 'case-result') {
    current.live = false;
    const task = head(frame.callback_origin).then(result => emit('listener-after-settlement', {case: frame.case, ...result})).finally(() => work.delete(task)); work.add(task);
  }
  const allowed = ['guest-ready', 'case-start', 'mock-authorization-visited', 'case-result', 'matrix-complete', 'host-pipe-settled', 'late-host-result-ignored', 'guest-cleaned', 'guest-failure', 'guest-diagnostics', 'real-authorization-ready', 'real-result'];
  if (!allowed.includes(frame.type)) throw new Error('unexpected guest event');
  emit('guest-event', {record: frame});
}
app.whenReady().then(() => {
  emit('electron-ready', {mode, electron: process.versions.electron, node: process.version, platform: process.platform});
  const executable = path.join(process.env.SystemRoot, 'System32', 'wsl.exe');
  const repo = '/home/plumwsl/workspaces/pulsara_agent';
  const argv = ['-d', 'Ubuntu', '-u', 'plumwsl', '--cd', repo, '--exec', repo + '/.venv/bin/python', repo + '/output/windows-g0/pg_admission_r3/round6/guest.py', mode];
  child = spawn(executable, argv, {shell: false, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe']});
  emit('guest-carrier', {pid: child.pid, executable, argv});
  child.stdin.on('error', () => emit('guest-input-closed'));
  let pending = '';
  const decoder = new TextDecoder('utf-8', {fatal: true});
  child.stdout.on('data', chunk => {
    try {
      pending += decoder.decode(chunk, {stream: true}); let index;
      while ((index = pending.indexOf('\n')) >= 0) {const line = pending.slice(0, index); pending = pending.slice(index + 1); receive(JSON.parse(line));}
    } catch {
      emit('guest-protocol-invalid'); closing = true; current.live = false;
      if (!child.stdin.writableEnded) child.stdin.end();
    }
  });
  // The guest suppresses private SDK auth exception payloads through its original owner.
  // Keep only stderr byte counts here; never forward unknown secret-bearing carriers.
  let stderrBytes = 0;
  child.stderr.on('data', chunk => {stderrBytes += chunk.length;});
  child.once('error', () => {emit('guest-carrier-start-failed'); app.exit(1);});
  child.once('close', async (code, signal) => {
    closing = true; current.live = false;
    await Promise.allSettled([...work]);
    emit('guest-carrier-closed', {code, signal, stderrBytes, partialFrame: Boolean(pending)});
    fs.writeFileSync(path.join(__dirname, mode + '.host-results.json'), JSON.stringify({mode, records}, null, 2));
    app.exit(code === 0 && !pending ? 0 : 1);
  });
}).catch(() => {emit('electron-start-failed'); app.exit(1);});
