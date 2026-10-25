import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {build} from '../../frontend/node_modules/vite/dist/node/index.js';
import react from '../../frontend/node_modules/@vitejs/plugin-react/dist/index.js';
const here=path.dirname(fileURLToPath(import.meta.url));
const repo=path.resolve(here,'../..');
const read=p=>fs.readFile(path.join(repo,p),'utf8');
const result=await build({configFile:false,root:repo,define:{'process.env.NODE_ENV':'\"production\"'},plugins:[react()],resolve:{alias:{react:path.join(repo,'frontend/node_modules/react'), 'react-dom':path.join(repo,'frontend/node_modules/react-dom')}},build:{write:false,lib:{entry:path.join(here,'preview.tsx'),formats:['iife'],name:'PulsaraPreview',cssFileName:'preview',fileName:'preview'},cssCodeSplit:false,minify:true}});
const outputs=(Array.isArray(result)?result:[result]).flatMap(x=>x.output);
const js=outputs.filter(x=>x.type==='chunk').map(x=>x.code).join('\n');
const css=outputs.filter(x=>x.type==='asset'&&x.fileName.endsWith('.css')).map(x=>x.source).join('\n');
const startupHtml=await read('desktop/assets/startup.html');
const startupCss=await read('desktop/assets/startup.css');
const startupJs=(await read('desktop/assets/startup.js')).replace('new URLSearchParams(location.search)','new URLSearchParams(window.__previewParams)');
const logo='data:image/png;base64,'+(await fs.readFile(path.join(repo,'desktop/assets/pulsara.png'))).toString('base64');
const safeScript=s=>s.replace(/<\/script/gi,'<\\/script');
function startup(phase,error='',missing=false) {
 const mock=`window.__previewParams=${JSON.stringify({phase,error})};
 const notify=text=>parent.postMessage({preview:true,text},'*');
 window.pulsaraDesktop={platform:'win32',minimize:()=>notify('预览：最小化'),toggleMaximize:()=>notify('预览：最大化／还原'),closeWindow:()=>notify('预览：隐藏窗口'),quit:()=>notify('预览：退出'),retryStartup:()=>notify('预览：请求重试；不会连接实际后端'),showLogs:()=>notify('预览：打开日志'),openExternal:()=>notify('安装帮助：https://learn.microsoft.com/windows/wsl/install'),wslDiscover:async()=>({distros:${missing?'[]':JSON.stringify([{name:'Ubuntu',version:2,default:true}])}}),wslInspect:async()=>({distro:'Ubuntu',user:'plumwsl',home:'/home/plumwsl',uid:1000}),wslSelect:async()=>notify('预览：确认环境，开始安装随包资源；此演示不会实际安装')};`;
 return startupHtml.replace(/<meta http-equiv="Content-Security-Policy"[^>]*>/,'')
 .replace('<link rel="stylesheet" href="startup.css">',`<style>${startupCss}</style>`)
 .replace('src="pulsara.png"',`src="${logo}"`)
 .replace('<script src="startup.js"></script>',`<script>${safeScript(mock+'\n'+startupJs)}</script>`);
}
function component(mode){return `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><style>${css}</style><body><div id="root"></div><script>window.__previewMode=${JSON.stringify(mode)};${safeScript(js.replaceAll("/assets/pulsara-icon.png",logo))}</script></body></html>`;}
const pages=[
 ['首次设置','已有 WSL2 Ubuntu','startup.html + startup.js / 原页面，示例发行版与默认用户。',startup('setup')],
 ['首次设置','未发现 WSL2','同一设置页的空列表状态；安装帮助按钮在演示中仅显示链接。',startup('setup','',true)],
 ['启动与恢复','准备应用资源','同一启动页的安装进度文案。',startup('starting','正在校验并安装用户私有 Linux 应用资源…')],
 ['启动与恢复','启动／自动恢复期间','恢复由实际运行状态决定，此处展示共用的启动遮罩。',startup('starting')],
 ['启动与恢复','连接失败与重试','原失败页；展开技术详情、重试与日志按钮可试点。错误为示例。',startup('failed','无法连接本地工作台：示例连接错误')],
 ['启动与恢复','正在退出','同一启动页的正常退出等待状态。',startup('stopping','正在停止并结算当前工作，等待内核退出…')],
 ['启动与恢复','退出未完成','原失败页根据错误前缀切换标题与提示；示例错误。',startup('failed','退出尚未完成：后台工作尚未确认关闭（预览示例）')],
 ['启动与恢复','正在重置数据库','共用状态页；这里只展示，不执行重置。',startup('resetting','正在停止内核并重置专用数据库…')],
 ['启动与恢复','数据库重置失败','共用失败页；示例错误。',startup('failed','数据库重置未完成：示例错误，请查看日志')],
 ['工作目录','选择 Linux 工作目录','直接渲染 WorkspaceDirectoryPicker；可进入目录、新建示例文件夹、选择目录。',component('directory')],
 ['工作目录','目录为空','当前目录没有子文件夹，可使用工具栏新建。',component('directory-empty')],
 ['工作目录','读取目录失败','同一选择器的错误与重新读取状态；点击后展示正常示例目录。',component('directory-error')],
 ['工作目录','新建会话 → 选择目录','复用现有 NewSessionDialog；点击指定目录后可打开 Linux 选择器。',component('session')],
 ['设置','WSL 内命令行入口','直接渲染 CliSettings 的 Windows 分支；外层只保留简化的设置容器。',component('cli')],
];
const html=`<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Pulsara · Windows + WSL2 界面预览</title>
<style>*{box-sizing:border-box}body{margin:0;background:#e9e5db;color:#252722;font:14px 'Segoe UI','Microsoft YaHei',sans-serif}header{height:82px;background:#faf8f2;border-bottom:1px solid #d6d1c7;padding:17px 25px}header strong{font-size:19px}header p{font-size:12px;color:#77746d;margin:7px 0}.layout{display:grid;grid-template-columns:245px 1fr;height:calc(100vh - 82px)}aside{overflow:auto;background:#f5f2ea;padding:12px}aside h3{font-size:11px;color:#8a806e;margin:17px 12px 7px}aside button{display:block;width:100%;border:0;border-radius:8px;background:transparent;text-align:left;padding:11px 12px;color:#59564e;font:inherit;cursor:pointer}aside button.active{background:#e6ddca;color:#342b19;font-weight:600}section{display:flex;flex-direction:column;min-width:0;padding:20px;gap:12px}.caption{min-height:44px}.caption b{display:block;font-size:16px;margin-bottom:5px}.caption span{font-size:12px;color:#69655e}iframe{width:100%;flex:1;min-height:430px;border:1px solid #d5cfc2;border-radius:12px;background:#f3f0e8;box-shadow:0 9px 35px #30271b12}.notice{min-height:26px;font-size:12px;color:#74674e}button:focus-visible{outline:2px solid #996a24} @media(max-width:760px){.layout{grid-template-columns:185px 1fr}section{padding:10px}header{padding:15px}.layout{height:auto}iframe{height:700px;flex:auto}}</style>
<header><strong>Pulsara · Windows + WSL2</strong><p>当前源码界面预览（基于 alpha.10） · 真实页面／组件 + 示例数据 · 所有操作仅供演示，不安装、不写文件、不连接后端</p></header>
<div class="layout"><aside id="nav" aria-label="页面与状态"></aside><section><div class="caption"><b id="title"></b><span id="description"></span></div><div id="native-demo" hidden style="font-size:12px;color:#777"><label>“浏览…”演示结果 <select id="native-candidate"><option value="valid">有效：我的项目</option><option value="deep">有效：14 层长目录</option><option value="outside">无效：Linux /tmp</option><option value="windows">无效：Windows 目录</option><option value="other">无效：其他发行版</option><option value="cancel">取消选择</option></select></label> · HTML 模拟返回值，正式应用打开系统窗口</div><iframe id="preview" title="Pulsara 页面预览" sandbox="allow-scripts allow-same-origin allow-forms"></iframe><div class="notice" id="notice" role="status">选择左侧页面；按钮可以试点。</div><details style="font-size:12px;color:#777"><summary>哪些是新增，哪些是共用界面？</summary><p>新增专用界面：首次 WSL 环境设置、Linux 工作目录选择器。启动、恢复、失败、退出和重置复用同一遮罩；命令行入口是现有设置组件的 Windows 分支。新建会话、文件导入、HTML／图片／PDF 预览、OAuth、模型设置都是共用界面。Windows 使用系统原生标题栏与窗口控制按钮；此 HTML 仅展示窗口内容区，不模拟系统标题栏。主工作台工具栏位置与快捷键适配没有新增独立页面。</p></details></section></div>
<script>const pages=${JSON.stringify(pages).replace(/</g,String.fromCharCode(92)+'u003c')};const nav=document.getElementById('nav');let group;pages.forEach((p,i)=>{if(p[0]!==group){group=p[0];const h=document.createElement('h3');h.textContent=group;nav.append(h)}const b=document.createElement('button');b.textContent=p[1];b.onclick=()=>show(i);nav.append(b)});function show(i){const p=pages[i];document.getElementById('native-demo').hidden=!(i>=9&&i<=12);document.querySelectorAll('aside button').forEach((b,n)=>{b.classList.toggle('active',n===i);b.setAttribute('aria-current',n===i?'page':'false')});document.getElementById('title').textContent=p[1];document.getElementById('description').textContent=p[2];document.getElementById('notice').textContent='演示模式 · 不会操作实际环境';document.getElementById('preview').srcdoc=p[3]}window.addEventListener('message',e=>{if(e.source===document.getElementById('preview').contentWindow&&e.data?.preview)document.getElementById('notice').textContent=e.data.text});show(0);</script></html>`;
await fs.writeFile(path.join(here,'index.html'),html);
console.log(JSON.stringify({file:path.join(here,'index.html'),pages:pages.length,bytes:Buffer.byteLength(html)}));
