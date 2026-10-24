// Research-only Electron main; no production main, browser, provider, or database.
const {app}=require('electron');
const {spawn}=require('node:child_process');
const path=require('node:path');
app.setPath('userData',path.join(__dirname,'electron-probe-profile'));
app.commandLine.appendSwitch('disable-gpu');
app.whenReady().then(()=>{
  const child=spawn(process.argv[2],process.argv.slice(3),{shell:false,stdio:'ignore',windowsHide:true});
  console.log(JSON.stringify({event:'electron-owner-start',pid:process.pid,childPid:child.pid,versions:process.versions,execPath:process.execPath,userData:app.getPath('userData')}));
  child.on('exit',(code,signal)=>{console.log(JSON.stringify({event:'electron-child-exit',code,signal}));app.quit()});
});
