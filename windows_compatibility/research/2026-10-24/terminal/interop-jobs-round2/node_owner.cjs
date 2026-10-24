// Research only: exercise the actual locked Node/libuv global Job around owner.
const {spawn}=require('node:child_process');
const child=spawn(process.argv[2],process.argv.slice(3),{shell:false,stdio:'ignore',windowsHide:true});
console.log(JSON.stringify({event:'node-owner-start',pid:process.pid,childPid:child.pid,node:process.version,execPath:process.execPath}));
child.on('exit',(code,signal)=>{console.log(JSON.stringify({event:'node-child-exit',code,signal}));process.exitCode=code||0});
