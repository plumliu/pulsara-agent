const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const {createInterface} = require('node:readline');
const path = require('node:path');
const python = process.argv[2] || '/usr/bin/python3';
const env = {...process.env};
for (const key of Object.keys(env)) if (key.toUpperCase() === 'WSLENV') delete env[key];
const wsl = path.join(process.env.SystemRoot, 'System32', 'wsl.exe');
async function run(code, args, input, inspect) {
  const command = ['-d','Ubuntu','-u','plumwsl','--cd','/home/plumwsl','--exec',python,'-u','-c',code,...args];
  console.log(JSON.stringify({event:'command',file:wsl,args:command,node:process.version,python}));
  const child = spawn(wsl, command, {shell:false,stdio:['pipe','pipe','pipe'],env});
  const out=[], err=[]; const events=[];
  child.stdout.on('data', b=>out.push(b)); child.stderr.on('data', b=>err.push(b));
  child.on('exit',(code,signal)=>events.push({event:'exit',code,signal}));
  const lines=[]; createInterface({input:child.stdout}).on('line',l=>lines.push(l));
  await new Promise((resolve,reject)=> {child.on('error',reject);child.on('spawn',resolve)});
  child.stdin.on('error',e=>console.log(JSON.stringify({event:'stdin-error',code:e.code})));
  child.stdin.write(input.subarray(0,1)); child.stdin.end(input.subarray(1));
  const result=await new Promise(resolve=>child.on('close',(code,signal)=>resolve({code,signal})));
  const observation={...result,stdout:Buffer.concat(out),stderr:Buffer.concat(err),lines,events};
  inspect(observation);
  console.log(JSON.stringify({event:'pass',...result,stdoutBytes:observation.stdout.length,stderrBytes:observation.stderr.length,events,lines:lines.filter(l=>l.length<2048)}));
}
(async()=>{
  const literal=['中文 空格','"double"',"'single'",'back\\slash','', '$HOME; & | < >', '尾部\\', 'emoji😀'];
  const payload=Buffer.concat([Buffer.from('中文 空格\r\n😀\n'),Buffer.alloc(1024*1024,0x61)]);
  for(const exit of [0,1,7]) await run('import json,sys; data=sys.stdin.buffer.read(); sys.stderr.buffer.write(b"x"*(2*1024*1024)); sys.stderr.buffer.flush(); sys.stdout.buffer.write(data); sys.stdout.buffer.flush(); sys.exit(int(sys.argv[1]))',[String(exit)],payload,r=>{
    assert.equal(r.code,exit);assert.equal(r.signal,null);assert.deepEqual(r.stdout,payload);assert.equal(r.stderr.length,2*1024*1024);
  });
  await run('import json,os,sys,time; data=sys.stdin.buffer.read(); frame=(json.dumps({"type":"probe","argv":sys.argv[1:],"input":data.decode(),"python":sys.version,"uid":os.getuid()},ensure_ascii=False)+"\\n").encode(); [(os.write(1,bytes([b])),time.sleep(.001)) for b in frame]; os.write(2,"诊断😀\\n".encode())',literal,Buffer.from('分段输入中文😀'),r=>{
    assert.equal(r.code,0);assert.equal(r.lines.length,1);const frame=JSON.parse(r.lines[0]);assert.deepEqual(frame.argv,literal);assert.equal(frame.input,'分段输入中文😀');assert.equal(frame.uid,1000);assert.equal(r.stderr.toString(),'诊断😀\n');
  });
})().catch(e=>{console.error(e);process.exitCode=1});
