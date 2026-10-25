import React from '../../frontend/node_modules/react/index.js';
import {createRoot} from '../../frontend/node_modules/react-dom/client.js';
import {WorkspaceDirectoryPicker, pickLinuxWorkspaceDirectory} from '../../frontend/components/workspace-directory-picker';
import {NewSessionDialog} from '../../frontend/components/overlays';
import {CliSettings} from '../../frontend/components/cli-settings';
import '../../frontend/app/styles/base.css';
import '../../frontend/app/styles/overlays.css';
import '../../frontend/app/styles/settings.css';

const mode = (window as any).__previewMode;
const rootPath = '/home/plumwsl/workspaces';
const folders = new Map([
  [rootPath, ['Windows 中文验收', '我的项目', '资料整理']],
  [rootPath + '/我的项目', ['设计稿', '文档']],
]);
let missing = mode === 'directory-missing';
let failed = mode === 'directory-error';
const api = {
  async list(path = rootPath) {
    if (missing) throw Object.assign(new Error('工程根目录尚不存在，请先创建。'), {code:'WORKSPACE_ROOT_MISSING'});
    if (failed) { failed = false; throw new Error('无法读取目录，请刷新后重试。'); }
    return {root:rootPath, path, parent:path === rootPath ? null : path.slice(0,path.lastIndexOf('/')), entries:(folders.get(path) || []).map(name => ({name,path:path+'/'+name})),next_cursor:null};
  },
  async initialize() { missing = false; return {path:rootPath}; },
  async create(path:string,name:string) {
    if (!name.trim() || /[\\/]/.test(name) || name === '.' || name === '..') throw new Error('请输入单个文件夹名称。');
    const children = folders.get(path) || [];
    if(children.includes(name)) throw new Error('此目录已存在。');
    folders.set(path,[...children,name]); return {path:path+'/'+name};
  },
  async select(path:string) {return {path};},
};
const notify = (text:string) => { parent.postMessage({preview:true,text},'*'); };
(window as any).pulsaraDesktop = {platform:'win32', cliStatus:async()=>({state:'ready',managedBy:'wsl',path:'/home/plumwsl/.local/share/pulsara/runtime/0.1.0-alpha.10/bin/pulsara'})};
const root = createRoot(document.getElementById('root')!);
const background = <div style={{padding:32,color:'var(--muted)',fontSize:13}}>Pulsara · Windows / WSL2</div>;
const finish = (path:string|null) => notify(path ? '预览已选择：'+path : '预览已取消；可在左侧重新打开此页。');
if (mode.startsWith('directory')) {
  root.render(<>{background}<WorkspaceDirectoryPicker api={api} signal={new AbortController().signal} finish={finish}/></>);
} else if (mode === 'session') {
  root.render(<>{background}<NewSessionDialog open canCreateSession onClose={()=>notify('预览：关闭新建会话')} onCreate={async value=>{notify('预览：'+(value.kind==='quick'?'快速开始':value.path));return false;}} onPickDirectory={signal=>pickLinuxWorkspaceDirectory(api,signal)}/></>);
} else {
  root.render(<main style={{maxWidth:850,margin:'60px auto',padding:24}}><h2>设置</h2><CliSettings/></main>);
}
