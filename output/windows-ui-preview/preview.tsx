import React from '../../frontend/node_modules/react/index.js';
import {createRoot} from '../../frontend/node_modules/react-dom/client.js';
import {WorkspaceDirectoryPicker, pickLinuxWorkspaceDirectory} from '../../frontend/components/workspace-directory-picker';
import {NewSessionDialog} from '../../frontend/components/overlays';
import {CliSettings} from '../../frontend/components/cli-settings';
import '../../frontend/app/styles/base.css';
import '../../frontend/app/styles/overlays.css';
import '../../frontend/app/styles/settings.css';

const mode = (window as any).__previewMode;
const rootPath = '/home/plumwsl';
const folders = new Map([
  [rootPath, ['Documents', '我的项目', 'workspaces']],
  [rootPath + '/我的项目', ['设计稿', '文档']],
]);
const deepParts = ['workspaces','资料归档','2026年','季度项目','课题研究','数据分析','原始数据','实验记录','第一批次','处理结果','图表与报告','包含中文与英文的很长文件夹名称 Long directory name','附件','最终版本'];
let deepPath = rootPath;
for (const part of deepParts) {
  const children = folders.get(deepPath) || [];
  if (!children.includes(part)) folders.set(deepPath,[...children,part]);
  deepPath += '/'+part;
}
folders.set(deepPath,['导出结果','说明文档']);
let failed = mode === 'directory-error';
const api = {
  async chooseNative() {
    notify('预览模拟原生选择结果；正式应用会打开 Windows 目录窗口。');
    const selected = (parent.document.getElementById('native-candidate') as HTMLSelectElement)?.value || 'valid';
    if (selected === 'cancel') return null;
    if (selected === 'deep') return {path:'\\\\wsl.localhost\\Ubuntu'+deepPath.replaceAll('/','\\'),linuxPath:deepPath};
    if (selected === 'windows') return {path:'C:\\Users\\plumlocal\\Documents',linuxPath:null};
    if (selected === 'other') return {path:'\\\\wsl.localhost\\OtherUbuntu\\home\\plumwsl',linuxPath:null};
    if (selected === 'outside') return {path:'\\\\wsl.localhost\\Ubuntu\\tmp',linuxPath:'/tmp'};
    return {path:'\\\\wsl.localhost\\Ubuntu\\home\\plumwsl\\我的项目',linuxPath:rootPath+'/我的项目'};
  },
  async list(path = rootPath) {
    if (failed) { failed = false; throw new Error('无法读取目录，请刷新后重试。'); }
    if (path !== rootPath && !path.startsWith(rootPath+'/')) throw new Error('该目录无效，请选择 '+rootPath+' 下的文件夹。');
    return {root:rootPath, path, parent:path === rootPath ? null : path.slice(0,path.lastIndexOf('/')), entries:(mode === 'directory-empty' ? [] : folders.get(path) || []).map(name => ({name,path:path+'/'+name})),next_cursor:null};
  },
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
