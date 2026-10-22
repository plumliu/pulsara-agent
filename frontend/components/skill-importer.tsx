import { useEffect, useId, useState } from 'react';
import { Check, FolderOpen, LoaderCircle, X } from 'lucide-react';
import type { SkillImportCandidate, SkillImportInput } from '../lib/pulsara-types';

export function SkillImporter({ onPreview, onInstall, onPickDirectory, onClose, scopeLabel = '添加到这台设备' }: {
  onPreview: (sourcePath: string) => Promise<SkillImportCandidate[]>;
  onInstall: (input: SkillImportInput) => Promise<boolean>;
  onPickDirectory: () => Promise<string | null>;
  onClose: () => void;
  scopeLabel?: string;
}) {
  const sourceId = useId();
  const [path, setPath] = useState('');
  const [items, setItems] = useState<SkillImportCandidate[]>();
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [installed, setInstalled] = useState<Set<string>>(new Set());
  const [failures, setFailures] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const pickDirectory = async () => {
    setBusy(true); setError('');
    try {
      const selectedPath = await onPickDirectory();
      if (selectedPath !== null) { setPath(selectedPath); setItems(undefined); }
    } catch (cause) { setError(cause instanceof Error ? cause.message : '无法打开目录选择器。'); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === 'Escape' && !busy) onClose(); };
    window.addEventListener('keydown', close);
    return () => window.removeEventListener('keydown', close);
  }, [busy, onClose]);
  const preview = async () => {
    setBusy(true); setError('');
    try {
      const result = await onPreview(path.trim());
      setItems(result); setSelected(new Set(result.map((item) => item.sourcePath))); setInstalled(new Set()); setFailures({});
      if (!result.length) setError('这个目录中未找到技能。请选择含 SKILL.md 的目录，或 skills 来源集合。');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '无法读取来源目录。'); }
    finally { setBusy(false); }
  };
  const install = async () => {
    setBusy(true); setError(''); setFailures({});
    try {
      // Independent candidates: failure never implies rollback of earlier installs.
      for (const item of items ?? []) {
        if (!selected.has(item.sourcePath) || installed.has(item.sourcePath)) continue;
        if (!item.name.trim() || !item.description.trim()) {
          setFailures((current) => ({ ...current, [item.sourcePath]: '请补充安装名称和用途描述后重试。' }));
          continue;
        }
        try {
          if (await onInstall(item)) setInstalled((current) => new Set([...current, item.sourcePath]));
          else setFailures((current) => ({ ...current, [item.sourcePath]: '这项技能未安装，可修正后重试。' }));
        } catch (cause) {
          setFailures((current) => ({ ...current, [item.sourcePath]: cause instanceof Error ? cause.message : '这项技能未安装，请重试。' }));
        }
      }
    } finally { setBusy(false); }
  };
  const edit = (sourcePath: string, field: 'name' | 'description', value: string) => {
    setItems((current) => current?.map((item) => item.sourcePath === sourcePath ? { ...item, [field]: value } : item));
  };
  const ready = items?.some((item) => selected.has(item.sourcePath) && !installed.has(item.sourcePath));
  return <div className="capability-add-panel capability-add-panel--skill" role="dialog" aria-modal="true" aria-label="导入技能" aria-busy={busy}>
    <div className="capability-add-panel__backdrop" onClick={busy ? undefined : onClose} />
    <section><header><div><span>{scopeLabel}</span><h2>导入技能</h2></div><button aria-label="关闭" disabled={busy} onClick={onClose}><X size={16} /></button></header>
      <div className="capability-dialog-body">
      <div className="capability-field"><span id={`${sourceId}-label`}>来源目录</span><button className="capability-directory-picker" type="button" autoFocus aria-labelledby={`${sourceId}-label ${sourceId}-path`} disabled={busy} onClick={() => void pickDirectory()}><FolderOpen size={16} /><span id={`${sourceId}-path`}>{path || '选择目录'}</span></button></div>
      <button className="secondary-action" disabled={busy || !path.trim()} onClick={() => void preview()}><FolderOpen size={14} />读取预览</button>
      {error && <p role="alert">{error}</p>}
      {items?.map((item) => <div className="skill-import-candidate" key={item.sourcePath}>
        <label className="capability-choice"><input type="checkbox" checked={selected.has(item.sourcePath)} disabled={busy || installed.has(item.sourcePath)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(item.sourcePath); else next.delete(item.sourcePath); return next; })} /><span className="capability-source-path">{item.sourcePath}</span></label>
        {installed.has(item.sourcePath) ? <p><Check size={14} />已安装</p> : <>
          <label className="capability-field"><span>安装名称</span><input value={item.name} disabled={busy} onChange={(event) => edit(item.sourcePath, 'name', event.target.value)} /></label>
          <label className="capability-field"><span>用途描述</span><textarea value={item.description} disabled={busy} onChange={(event) => edit(item.sourcePath, 'description', event.target.value)} /></label>
          {item.details.map((detail, index) => <small key={index}>{detail}</small>)}
          {failures[item.sourcePath] && <p role="alert">{failures[item.sourcePath]}</p>}
        </>}
      </div>)}
      </div>
      <footer><button className="secondary-action" disabled={busy} onClick={onClose}>关闭</button><button className="capability-primary" disabled={busy || !ready} onClick={() => void install()}>{busy ? <LoaderCircle size={14} /> : <Check size={14} />}安装所选技能</button></footer>
    </section>
  </div>;
}
