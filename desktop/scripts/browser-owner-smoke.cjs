const {randomIdentity,deriveIdentity,isCanonicalIdentity}=require('../dist/identity');
// Real Electron integration test. Uses only local fixtures and a disposable profile.
const {app,BrowserWindow}=require('electron');
const {BrowserOwner}=require('../dist/browser-owner');
const {browserGuestSource}=require('../dist/browser-contract');
const fs=require('node:fs'),os=require('node:os'),path=require('node:path'),http=require('node:http'),assert=require('node:assert/strict');
const output=process.env.PULSARA_BROWSER_SMOKE_OUTPUT || path.resolve(__dirname,'../../output/playwright/browser-owner-integration');fs.mkdirSync(output,{recursive:true});
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'pulsara-owner-test-'));
app.setPath('userData',profile);app.setPath('sessionData',path.join(profile,'sessions'));
app.on('window-all-closed',()=>{});
const report={tests:[],events:[],requests:[],profile,versions:process.versions};let win,owner;const servers=[];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function until(fn){const end=Date.now()+10000;while(!fn()){if(Date.now()>end)throw new Error('condition timed out');await sleep(20)}}
async function test(name,fn){try{const detail=await fn();report.tests.push({name,pass:true,detail});console.log('PASS',name)}catch(e){report.tests.push({name,pass:false,error:e.stack});console.error('FAIL',name,e)}}
async function server(fn){const s=http.createServer(fn);servers.push(s);await new Promise(r=>s.listen(0,'127.0.0.1',r));return 'http://127.0.0.1:'+s.address().port}
function page(s){return owner.pages.get(s.instanceId)}
function workbench(origin, emit = () => {}) {
 const host=new BrowserWindow({show:false,width:1000,height:800,webPreferences:{webviewTag:true,sandbox:true,contextIsolation:true,nodeIntegration:false}});
 return [host,new BrowserOwner(host,()=>origin,emit)];
}
async function mount(s, host=win, resources=owner, attributes={}) {
 await host.webContents.executeJavaScript(`{
  const panel=document.createElement('section');
  panel.id=${JSON.stringify(s.instanceId)};
  panel.style='position:absolute;inset:30px auto auto 300px;width:600px;height:500px;overflow:hidden';
  const guest=document.createElement('webview');
  for (const [key,value] of Object.entries(${JSON.stringify(attributes)})) guest.setAttribute(key,value);
  guest.style='width:100%;height:100%';
  guest.src=${JSON.stringify(browserGuestSource(s.instanceId))};
  panel.append(guest);document.body.append(panel);
 }`);
 await until(()=>resources.pages.get(s.instanceId)?.attached);
 return resources.pages.get(s.instanceId).contents;
}
async function open(){
 const state=owner.create({tabId:randomIdentity('tab')});
 await mount(state);
 return state;
}
async function nav(s,url){owner.command({instanceId:s.instanceId,kind:'navigate',url});await until(()=>{const p=page(s);return !p.state.loading&&(p.state.url===url||p.state.error)});await sleep(30);return page(s).state}
async function main(){
 const protectedOrigin=await server((req,res)=>{report.requests.push({protected:true,url:req.url});res.end('protected')});
 const origin=await server((req,res)=>{report.requests.push({url:req.url,method:req.method});if(req.url==='/hang')return;if(req.url==='/redirect'){res.writeHead(302,{Location:'/next'});res.end();return}if(req.url==='/sw.js'){res.setHeader('Content-Type','application/javascript');res.end(`oninstall=()=>skipWaiting();onactivate=e=>e.waitUntil(clients.claim());onmessage=e=>e.waitUntil(new Promise(r=>setTimeout(r,250)).then(()=>fetch(e.data)).catch(()=>{}));`);return}if(req.url==='/worker.js'){res.setHeader('Content-Type','application/javascript');res.end(`onmessage=e=>fetch(e.data).then(()=>postMessage('allowed'),()=>postMessage('blocked'));`);return}res.setHeader('Content-Type','text/html');res.end(`<title>Fixture ${req.url}</title><input id="input"><form id="form" method="post" action="/post"><input name="x" value="body"></form><div style="height:3000px">fixture</div><script>window.nonce=crypto.randomUUID()</script>`)});
 [win,owner]=workbench(protectedOrigin,event=>report.events.push(event));
 await win.loadURL(protectedOrigin);
 await test('shared canonical identity matches Python Unicode vector in Electron',async()=>{assert.equal(deriveIdentity('entry','fixture','中文🙂','a:b/c','','e\u0301'),'entry:hbumehpzj53h3gbt572hhjtpbq');assert.equal(isCanonicalIdentity('entry:'+ 'a'.repeat(25)+'b','entry'),false)});
 const a=owner.create({tabId:randomIdentity('tab')});
 let wc;
 await test('one trusted reservation attaches a sandboxed guest in the owner partition and queues early navigation',async()=>{
  assert.equal(a.url,'');
  assert.equal(page(a).contents,undefined);
  assert.ok(isCanonicalIdentity(a.instanceId,'browser-instance'));
  owner.command({instanceId:a.instanceId,kind:'navigate',url:origin+'/'});
  wc=await mount(a,win,owner,{partition:'persist:untrusted',preload:'file:///invalid-preload.js',nodeintegration:'',disablewebsecurity:'',webpreferences:'webviewTag, allowRunningInsecureContent'});
  await until(()=>page(a).state.url===origin+'/'&&!page(a).state.loading);
  assert.equal(wc.session,owner.partitionState.session);
  assert.notEqual(wc.session,win.webContents.session);
  const prefs=wc.getLastWebPreferences();
  for(const key of ['sandbox','contextIsolation','webSecurity','disableDialogs']) assert.equal(prefs[key],true);
  for(const key of ['nodeIntegration','nodeIntegrationInWorker','nodeIntegrationInSubFrames','webviewTag','allowRunningInsecureContent']) assert.equal(prefs[key],false);
  const capabilities=await wc.executeJavaScript('({node:typeof require,bridge:typeof pulsaraDesktop,process:typeof process})');
  assert.deepEqual(capabilities,{node:'undefined',bridge:'undefined',process:'undefined'});
  assert.equal(page(a).state.canGoBack,false);
 });
 await test('unreserved, duplicate, and closed reservation attaches do not create guests',async()=>{
  assert.throws(()=>owner.create({tabId:'tab:'+ 'a'.repeat(25)+'b'}));
  assert.throws(()=>owner.close(randomIdentity('tab')));
  owner.close(randomIdentity('browser-instance'));
  const closed=owner.create({tabId:randomIdentity('tab')});
  owner.close(closed.instanceId);
  const sources=[origin+'/unreserved',browserGuestSource(a.instanceId),browserGuestSource(closed.instanceId)];
  await win.webContents.executeJavaScript(`{
   for (const url of ${JSON.stringify(sources)}) {
    const other=document.createElement('webview');other.src=url;document.body.append(other);
   }
  }`);
  await sleep(150);
  assert.equal(owner.partitionState.contents.size,1);
  assert.equal(page(a).contents,wc);
  assert.equal(report.requests.some(request=>request.url==='/unreserved'),false);
 });
 await test('DOM collapse and movement preserve the same guest and document state',async()=>{
  await wc.executeJavaScript("document.querySelector('input').value='draft';scrollTo(0,600)");
  const before=await wc.executeJavaScript('({nonce,value:document.querySelector("input").value,y:scrollY})');
  const panel=`document.getElementById(${JSON.stringify(a.instanceId)})`;
  await win.webContents.executeJavaScript(`${panel}.hidden=true`);
  assert.equal(await win.webContents.executeJavaScript(`${panel}.firstElementChild.getBoundingClientRect().height`),0);
  await win.webContents.executeJavaScript(`{${panel}.style.left='170px';${panel}.hidden=false;}`);
  assert.equal(await win.webContents.executeJavaScript(`${panel}.firstElementChild.getBoundingClientRect().left`),170);
  assert.equal(page(a).contents,wc);
  assert.deepEqual(await wc.executeJavaScript('({nonce,value:document.querySelector("input").value,y:scrollY})'),before);
  return before;
 });
 await test('independent tabs share user browser storage and closing one preserves the partition',async()=>{
  await wc.executeJavaScript("localStorage.value='user';document.cookie='cookie=user;path=/'");
  const a2=await open(),b=await open();
  await nav(a2,origin+'/');await nav(b,origin+'/');
  assert.equal(await page(a2).contents.executeJavaScript('localStorage.value'),'user');
  assert.equal(await page(b).contents.executeJavaScript('localStorage.value'),'user');
  assert.notEqual(page(a2).contents,page(b).contents);
  assert.equal(page(a2).partition,page(b).partition);
  owner.close(a2.instanceId);owner.close(b.instanceId);
  assert.ok(owner.partitionState);
 });
 await test('native redirect, SPA, POST and history observed',async()=>{owner.command({instanceId:a.instanceId,kind:'navigate',url:origin+'/redirect'});await until(()=>page(a).state.url===origin+'/next'&&!page(a).state.loading);await wc.executeJavaScript("history.pushState({},'', '/spa#one')",true);await until(()=>page(a).state.url===origin+'/spa#one');owner.command({instanceId:a.instanceId,kind:'back'});await until(()=>page(a).state.url===origin+'/next');await wc.executeJavaScript("document.querySelector('form').submit()",true);await until(()=>page(a).state.url===origin+'/post'&&!page(a).state.loading);assert.ok(report.requests.some(r=>r.url==='/post'&&r.method==='POST'));return page(a).state});
 await test('slow navigation returns immediately and stop is scoped; genuine failure retry works',async()=>{owner.command({instanceId:a.instanceId,kind:'navigate',url:origin+'/hang'});await sleep(100);owner.command({instanceId:a.instanceId,kind:'stop'});await sleep(100);assert.equal(page(a).state.error,null);assert.equal(page(a).state.loading,false);await nav(a,'http://127.0.0.1:1/fail');assert.ok(page(a).state.error);await nav(a,origin+'/');assert.equal(page(a).state.error,null)});
 await test('permission denied; popup forwarded original owner without native child',async()=>{assert.equal(await wc.executeJavaScript('Notification.requestPermission()'),'denied');const count=BrowserWindow.getAllWindows().length;await wc.executeJavaScript(`window.open(${JSON.stringify(origin+'/popup')},'_blank')`,true);await until(()=>report.events.some(e=>e.kind==='popup'));assert.equal(BrowserWindow.getAllWindows().length,count);const e=report.events.find(e=>e.kind==='popup');assert.equal('sessionId' in e,false);assert.equal(e.instanceId,a.instanceId);return e});
 await test('control port blocked for explicit navigation fetch websocket and worker',async()=>{const target=protectedOrigin+'/control';assert.throws(()=>owner.command({instanceId:a.instanceId,kind:'navigate',url:target}));const result=await wc.executeJavaScript(`(async()=>{const u=${JSON.stringify(target)};return {fetch:await fetch(u).then(()=>true,()=>false),ws:await new Promise(r=>{const w=new WebSocket(u.replace('http:','ws:'));w.onerror=()=>r(false);w.onopen=()=>r(true)}),worker:await new Promise(r=>{const w=new Worker('/worker.js');w.onmessage=e=>{w.terminate();r(e.data)};w.postMessage(u)})}})()`);assert.deepEqual(result,{fetch:false,ws:false,worker:'blocked'});assert.equal(report.requests.some(r=>r.protected&&r.url==='/control'),false);return result});
 await test('user browser clear joins earlier tab close and host retirement without clearing storage early',async()=>{
  const [host,resources]=workbench(protectedOrigin);await host.loadURL(protectedOrigin);
  const first=resources.create({tabId:randomIdentity('tab')}),second=resources.create({tabId:randomIdentity('tab')});
  await mount(first,host,resources);await mount(second,host,resources);
  const part=resources.pages.get(first.instanceId).partition,old=resources.pages.get(first.instanceId).contents,other=resources.pages.get(second.instanceId).contents;
  const physicalClose=old.close.bind(old),physicalClear=part.session.clearData.bind(part.session);
  let requested,finished=false,cleared=false;
  old.close=options=>{requested=options};resources.close(first.instanceId);
  part.session.clearData=async(...args)=>{assert.equal(old.isDestroyed(),true);assert.equal(other.isDestroyed(),true);cleared=true;return physicalClear(...args)};
  const clearing=resources.clear(),retiring=resources.retireHost().then(()=>{finished=true});
  try{
   await new Promise(resolve=>setImmediate(resolve));assert.ok(requested);assert.equal(finished,false);assert.equal(cleared,false);assert.throws(()=>resources.create({tabId:randomIdentity('tab')}));
   old.close=physicalClose;physicalClose(requested);await Promise.all([clearing,retiring]);
   assert.equal(old.isDestroyed(),true);assert.equal(other.isDestroyed(),true);assert.equal(cleared,true);assert.equal(resources.pages.size,0);assert.equal(resources.partitionState,undefined);
  }finally{old.close=physicalClose;if(!old.isDestroyed())physicalClose({waitForBeforeUnload:false});await Promise.allSettled([clearing,retiring]);part.session.clearData=physicalClear;host.destroy()}
 });
 await test('clear tabless user partition rejects pending worker and a fresh user browser has empty storage',async()=>{await wc.executeJavaScript(`(async()=>{await navigator.serviceWorker.register('/sw.js');const reg=await navigator.serviceWorker.ready;reg.active.postMessage(${JSON.stringify(origin+'/late-worker')})})()`);const old=page(a).partition;owner.close(a.instanceId);await until(()=>wc.isDestroyed());await owner.clear();assert.equal(old.retired,true);const fresh=await open();assert.notEqual(page(fresh).partition,old);await nav(fresh,origin+'/');assert.equal(await page(fresh).contents.executeJavaScript('localStorage.value'),undefined);await sleep(400);assert.equal(report.requests.some(r=>r.url==='/late-worker'),false)});
 await test('crash destroys native resource and sends retained error state',async()=>{const s=await open();await nav(s,origin+'/');const wc=page(s).contents;wc.forcefullyCrashRenderer();await until(()=>report.events.some(e=>e.kind==='state'&&e.state.instanceId===s.instanceId&&e.state.crashed));assert.equal(owner.pages.has(s.instanceId),false);assert.equal(wc.isDestroyed(),true)});
 await test('host reload retires the user partition including tabless resources',async()=>{const part=owner.partitionState;await win.loadURL('data:text/html,reloaded');await owner.retireHost();assert.equal(part.retired,true);assert.equal(owner.partitionState,undefined);assert.throws(()=>owner.create({tabId:randomIdentity('tab')}));});
 await test('destroying host window closes its separately owned WebContents',async()=>{const [other,resources]=workbench(protectedOrigin);await other.loadURL(protectedOrigin);const s=resources.create({tabId:randomIdentity('tab')});const native=await mount(s,other,resources);other.destroy();await resources.retireHost();assert.equal(native.isDestroyed(),true);assert.equal(resources.pages.size,0);assert.equal(resources.partitionState,undefined)});

}
app.whenReady().then(main).catch(e=>{report.fatal=e.stack;console.error(e)}).finally(async()=>{await owner?.retireHost();if(win&&!win.isDestroyed()){const destroyed=new Promise(resolve=>win.webContents.once('destroyed',resolve));win.destroy();await destroyed}await Promise.all(servers.map(s=>new Promise(resolve=>{s.closeAllConnections();s.close(resolve)})));report.profileCleanup='Idle profile retained after Electron exit; removing an in-use native profile is not resource join.';report.summary={passed:report.tests.filter(t=>t.pass).length,failed:report.tests.filter(t=>!t.pass).length};fs.writeFileSync(path.join(output,'results.json'),JSON.stringify(report,null,2));console.log(report.summary);app.exit(report.fatal||report.summary.failed?1:0)});
