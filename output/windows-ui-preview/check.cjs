const {app,BrowserWindow}=require('electron');
const fs=require('node:fs');
const path=require('node:path');
app.setPath('userData',path.join(__dirname,'test-profile'));
app.whenReady().then(async()=>{
 const win=new BrowserWindow({width:1380,height:980,show:false,webPreferences:{contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:false,offscreen:true}});
 const errors=[];
 win.webContents.on('console-message',details=>{if(details.level==='error')errors.push(details.message)});
 await win.loadFile(path.join(__dirname,'index.html'));
 const run=code=>win.webContents.executeJavaScript(code);
 const results=[];
 for(let i=0;i<14;i++){
   await run(`new Promise(resolve=>{const f=document.getElementById('preview');f.addEventListener('load',()=>setTimeout(resolve,400),{once:true});show(${i})})`);
   const result=await run(`(()=>{const d=document.getElementById('preview').contentDocument;return {title:document.getElementById('title').textContent,text:d.body.innerText,modal:!!d.querySelector('dialog[open]')}})()`);
   if(!result.text.trim())throw Error('Blank page '+i);
   if(i>=9&&i<=11&&!result.modal)throw Error('Missing picker '+i);
   if(i===0||i===9)fs.writeFileSync(path.join(__dirname,i===0?'first-setup.png':'directory-picker.png'),(await win.webContents.capturePage()).toPNG());
   results.push({index:i,title:result.title,rendered:true});
 }
 await run(`new Promise(resolve=>{document.getElementById('preview').addEventListener('load',()=>setTimeout(resolve,120),{once:true});show(9)})`);
 await run(`(()=>{const d=document.getElementById('preview').contentDocument;Array.from(d.querySelectorAll('button')).find(b=>b.textContent==='我的项目').click()})()`);
 await new Promise(r=>setTimeout(r,120));
 if(!await run(`document.getElementById('preview').contentDocument.querySelector('.linux-directory-path').textContent.endsWith('/我的项目')`))throw Error('Navigation failed');
 if(errors.length)throw Error(errors.join('\n'));
 fs.writeFileSync(path.join(__dirname,'checks.json'),JSON.stringify({pages:results,directoryNavigation:true,consoleErrors:errors},null,2));
 console.log('14 pages rendered; directory navigation passed; no console errors');
 win.destroy();app.quit();
}).catch(e=>{console.error(e);app.exit(1)});
