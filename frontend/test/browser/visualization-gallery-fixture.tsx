import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { VisualizationGallery } from '../../components/visualization-gallery';
import '../../app/globals.css';
import '../../app/styles/base.css';
import '../../app/styles/workbench.css';
import '../../app/styles/shell.css';
import '../../app/styles/responsive.css';
import type { VisualizationOccurrence } from '../../lib/pulsara-types';

const html = (body: string, style = '', script = '') => `<!doctype html><meta charset="utf-8"><style>body{margin:0;background:#fcfaf5;color:#38352e;font:16px/1.6 system-ui}main{padding:24px;box-sizing:border-box}h1{font-size:24px;margin:0 0 12px}button,input{font:inherit}button{padding:5px 12px;background:#ede2cd;border:1px solid #ac8a52;border-radius:6px}svg{display:block} ${style}</style>${body}<script>${script}</script>`;
export const galleryFixtures = [
  html('<main><h1>群体结构 · 响应式视图</h1><label>筛选 <input aria-label="图内筛选" value="全部客户"></label><button id="count">点击 0</button><svg viewBox="0 0 740 270" aria-label="客户群体散点图">' + Array.from({length: 120}, (_, i) => `<circle cx="${45 + (i * 71) % 650}" cy="${35 + (i * 31) % 190}" r="${5 + i % 4}" fill="${['#aa7a34','#719589','#b2aaa0'][i%3]}" opacity=".8"/>`).join('') + '</svg></main>', 'svg{width:100%;height:auto;max-height:280px}input{max-width:160px;margin-right:12px}', "let n=0;document.querySelector('#count').onclick=e=>e.target.textContent='点击 '+(++n)"),
  html('<main style="width:6000px;height:280px;background:linear-gradient(90deg,#ede3ce,#8eadad)"><h1>6000px 宽图</h1><div style="position:absolute;left:5860px;top:160px">宽图最右端</div></main>'),
  html('<main style="height:10000px;background:linear-gradient(#efe1bf,#dbe8df)"><h1>10000px 长报告</h1><label>报告输入 <input aria-label="报告输入"></label><div style="padding-top:9800px">报告末尾</div></main>'),
  html('<main data-pulsara-visualization-root style="width:240px;height:150px;background:#e7d4ad"><h1>小卡片</h1><p>仅裁去外围留白</p></main>', 'body{padding:30px;background:#516475}'),
  html('<main style="height:100vh;display:flex;flex-direction:column"><h1>100vh 动态视窗</h1><div style="flex:1;min-height:0;background:#b4c6bb">随主视窗调整</div><footer>底部始终可见</footer></main>'),
  html('<main><h1>高密度表格</h1><table>' + Array.from({length:2500},(_,i)=>`<tr><td>记录 ${i}</td><td>${i*1.27}</td><td>这是一条比较长的详情</td></tr>`).join('') + '</table></main>'),
  html('<main><h1>动态主体</h1><button id="grow">增加尺寸</button><div data-pulsara-visualization-root id="root" style="width:200px;height:100px;background:#d9bc87">动态根</div></main>', '', "document.querySelector('#grow').onclick=()=>document.querySelector('#root').style.height='1200px'"),
  html('<main><h1>隔离检查</h1><a id="escape" href="https://example.invalid/escape" target="_top">顶层导航</a><div id="outcome"></div></main>', '', "try{top.document.body.innerHTML='BROKEN'}catch(e){document.querySelector('#outcome').textContent='DOM 隔离生效'}fetch('https://example.invalid/leak').catch(()=>{});window.open('https://example.invalid/popup');"),
];
const params = new URLSearchParams(location.search);
const count = Number(params.get('count') ?? 10);
const items: VisualizationOccurrence[] = Array.from({length:count},(_,ordinal)=>({ordinal, sourceFilename: ordinal === 0 ? '客户行为群体结构与规模（跨季度、含退款与随机噪声的完整分析）.html' : `view-${ordinal + 1}.html`, state: ordinal === 8 ? 'FAILED' : 'READY', visualizationRef:`sha256:fixture-${ordinal}`,contentSize: 12,failureDetail:ordinal===8?'示例文件未能读取':undefined}));
const reads: number[] = [];
Object.assign(window, {galleryReads:reads, galleryFixtures});
const read = async (_entry: string, ordinal: number) => {
  reads.push(ordinal);
  if (params.has('slow')) await new Promise(resolve=>setTimeout(resolve,ordinal%2?15:350));
  if (ordinal===9) throw new Error('read failed');
  return galleryFixtures[ordinal%galleryFixtures.length];
};
const thumbnail = async (_entry: string, ordinal: number, _digest: string, _size: number, signal: AbortSignal) => {
  const directory = import.meta.url.slice(0, import.meta.url.lastIndexOf('/') + 1);
  const response=await fetch(`${directory}generated-thumbnails/${ordinal%galleryFixtures.length}.png`,{signal});
  if(!response.ok)throw new Error('thumbnail unavailable');
  const blob=await response.blob();
  return new Promise<string>((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result));reader.onerror=reject;reader.readAsDataURL(blob);});
};
createRoot(document.getElementById('root')!).render(<StrictMode><div style={{height:'100dvh',overflow:'auto',containerType:'inline-size'}}>
  <div className="thread-column" style={{maxWidth:740,padding:'30px 0',margin:'auto'}}><article className="assistant-turn">
    <div className="assistant-copy"><h2>Pulsara</h2><p>这组可视化覆盖响应式、超宽、长页面与独立交互。</p></div>
    <VisualizationGallery entryId="fixture" items={items} onRead={read} onReadThumbnail={thumbnail}/>
    <div className="assistant-copy">后续正文位置</div>
  </article></div></div></StrictMode>);
