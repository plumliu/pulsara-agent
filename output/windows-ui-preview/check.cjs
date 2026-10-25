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
   const result=await run(`(()=>{const d=document.getElementById('preview').contentDocument;return {title:document.getElementById('title').textContent,text:d.body.innerText,modal:!!d.querySelector('dialog[open]'),customControlsVisible:!!d.querySelector('#window-controls:not([hidden])')}})()`);
   if(!result.text.trim())throw Error('Blank page '+i);
   if(result.customControlsVisible)throw Error('Duplicate Windows controls '+i);
   if(i===0){
     const setup=await run(`(()=>{const d=document.getElementById('preview').contentDocument;return {ready:!d.getElementById('wsl-confirm').disabled,identityHidden:d.getElementById('wsl-identity').hidden,refreshLabel:d.getElementById('wsl-refresh').getAttribute('aria-label'),refreshText:d.getElementById('wsl-refresh').textContent.trim()}})()`);
     if(!setup.ready||!setup.identityHidden||setup.refreshLabel!=='重新检查'||setup.refreshText)throw Error('Setup controls not ready');
     await run(`(async()=>{const w=document.getElementById('preview').contentWindow,d=w.document;const inspect=w.pulsaraDesktop.wslInspect;w.pulsaraDesktop.wslInspect=async()=>{throw new Error('示例：默认用户不可用')};d.getElementById('wsl-refresh').click();await new Promise(r=>setTimeout(r,20));if(d.getElementById('wsl-identity').hidden||!d.getElementById('wsl-confirm').disabled)throw Error('Inspection failure was hidden');w.pulsaraDesktop.wslInspect=inspect;d.getElementById('wsl-refresh').click();await new Promise(r=>setTimeout(r,20));if(!d.getElementById('wsl-identity').hidden||d.getElementById('wsl-confirm').disabled)throw Error('Refresh did not recover');d.getElementById('wsl-confirm').click();await new Promise(r=>setTimeout(r,20));})()`);
     const submitted=await run(`(()=>{const d=document.getElementById('preview').contentDocument;return {notice:document.getElementById('notice').textContent,value:d.getElementById('wsl-distribution').value,valid:d.getElementById('wsl-setup').checkValidity(),disabled:d.getElementById('wsl-confirm').disabled}})()`);
     if(!submitted.notice.includes('确认环境'))throw Error('Setup selection was not submitted: '+JSON.stringify(submitted));
     await run(`new Promise(resolve=>{document.getElementById('preview').addEventListener('load',()=>setTimeout(resolve,100),{once:true});show(0)})`);
   }
   if(i===1&&!await run(`document.getElementById('preview').contentDocument.getElementById('wsl-confirm').disabled`))throw Error('Missing WSL can be submitted');
   if(i>=9&&i<=11&&!result.modal)throw Error('Missing picker '+i);
   const capture={0:'first-setup.png',2:'starting.png',4:'connection-failed.png',6:'exit-incomplete.png',8:'reset-incomplete.png',9:'directory-picker.png',10:'directory-empty.png',11:'directory-error.png'}[i];
   if(capture)fs.writeFileSync(path.join(__dirname,capture),(await win.webContents.capturePage()).toPNG());
   if(i>=9&&i<=11){
     const bounds=await run(`(()=>{const f=document.getElementById('preview'),a=f.getBoundingClientRect(),b=f.contentDocument.querySelector('dialog').getBoundingClientRect();return {x:Math.floor(a.left+f.clientLeft+b.left),y:Math.floor(a.top+f.clientTop+b.top),width:Math.ceil(b.width),height:Math.ceil(b.height)}})()`);
     fs.writeFileSync(path.join(__dirname,capture.replace('.png','-modal.png')),(await win.webContents.capturePage(bounds)).toPNG());
   }
   results.push({index:i,title:result.title,rendered:true});
 }
 await run(`new Promise(resolve=>{document.getElementById('preview').addEventListener('load',()=>setTimeout(resolve,120),{once:true});show(9)})`);
 await run(`(()=>{const d=document.getElementById('preview').contentDocument,b=d.querySelector('button[aria-label="我的项目"]');b.click();b.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))})()`);
 await new Promise(r=>setTimeout(r,120));
 if(!await run(`document.getElementById('preview').contentDocument.querySelector('output[aria-label="所选文件夹"]').value.endsWith('/我的项目')`))throw Error('Navigation failed');
 const browseNative=async value=>{
   await run(`(()=>{document.getElementById('native-candidate').value=${JSON.stringify(value)};const d=document.getElementById('preview').contentDocument;[...d.querySelectorAll('button')].find(b=>b.textContent.includes('浏览…')).click()})()`);
   await new Promise(r=>setTimeout(r,120));
 };
 for(const candidate of ['outside','windows','other']){
   await browseNative(candidate);
   const state=await run(`(()=>{const d=document.getElementById('preview').contentDocument;return {covered:!!d.querySelector('.linux-directory-blocked'),disabled:[...d.querySelectorAll('button')].find(b=>b.textContent==='选择文件夹').disabled,path:d.querySelector('output').value,warning:d.querySelector('[role="alert"]').textContent,rows:d.querySelectorAll('.linux-directory-files li').length,logoReady:d.querySelector('.linux-directory-blocked img')?.naturalWidth>0}})()`);
   if(!state.covered||!state.disabled||!state.logoReady||state.rows||!state.warning.includes('/home/plumwsl'))throw Error('Invalid native selection was not covered: '+JSON.stringify(state));
   if(candidate==='outside'){
     const bounds=await run(`(()=>{const f=document.getElementById('preview'),a=f.getBoundingClientRect(),b=f.contentDocument.querySelector('dialog').getBoundingClientRect();return {x:Math.floor(a.left+f.clientLeft+b.left),y:Math.floor(a.top+f.clientTop+b.top),width:Math.ceil(b.width),height:Math.ceil(b.height)}})()`);
     fs.writeFileSync(path.join(__dirname,'directory-invalid-modal.png'),(await win.webContents.capturePage(bounds)).toPNG());
     await browseNative('cancel');
     if(!await run(`document.getElementById('preview').contentDocument.querySelector('output').value==='/tmp'`))throw Error('Native cancel lost the blocked selection');
   }
 }
 await browseNative('valid');
 if(!await run(`(()=>{const d=document.getElementById('preview').contentDocument;return !d.querySelector('.linux-directory-blocked')&&d.querySelector('output').value==='/home/plumwsl/我的项目'&&![...d.querySelectorAll('button')].find(b=>b.textContent==='选择文件夹').disabled})()`))throw Error('Valid native selection did not recover');
 await browseNative('deep');
 const deepDirectory=await run(`(()=>{const d=document.getElementById('preview').contentDocument,nav=d.querySelector('.linux-directory-location nav'),body=d.querySelector('.linux-directory-body');return {path:d.querySelector('output').value,ancestors:d.querySelectorAll('.linux-directory-places button').length,breadcrumbOverflow:nav.scrollWidth>nav.clientWidth,bodyOverflow:body.scrollHeight>body.clientHeight,breadcrumbScrollLeft:nav.scrollLeft}})()`);
 if(deepDirectory.ancestors!==15||!deepDirectory.breadcrumbOverflow||!deepDirectory.path.endsWith('/最终版本'))throw Error('Deep path did not render: '+JSON.stringify(deepDirectory));
 const deepBounds=await run(`(()=>{const f=document.getElementById('preview'),a=f.getBoundingClientRect(),b=f.contentDocument.querySelector('dialog').getBoundingClientRect();return {x:Math.floor(a.left+f.clientLeft+b.left),y:Math.floor(a.top+f.clientTop+b.top),width:Math.ceil(b.width),height:Math.ceil(b.height)}})()`);
 fs.writeFileSync(path.join(__dirname,'directory-deep-modal.png'),(await win.webContents.capturePage(deepBounds)).toPNG());
 fs.writeFileSync(path.join(__dirname,'deep-directory-check.json'),JSON.stringify(deepDirectory,null,2));
 if(errors.length)throw Error(errors.join('\n'));
 fs.writeFileSync(path.join(__dirname,'checks.json'),JSON.stringify({pages:results,directoryNavigation:true,nativeCandidates:true,invalidSelectionCover:true,consoleErrors:errors},null,2));
 console.log('14 pages rendered; directory navigation, native candidate validation and invalid selection cover passed; no console errors');
 win.destroy();app.quit();
}).catch(e=>{console.error(e);app.exit(1)});
