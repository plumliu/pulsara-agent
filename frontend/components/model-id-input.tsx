import { ChevronDown, LoaderCircle, RefreshCw } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import type { ModelDiscoveryInput, RuntimeAdapter } from '../lib/runtime-adapter';

interface Props {
  value: string;
  onChange: (value: string) => void;
  input: ModelDiscoveryInput;
  adapter: RuntimeAdapter;
}

/** Suggestions belong only to the exact connection draft that requested them. */
export function ModelIdInput({ value, onChange, input, adapter }: Props) {
  const id = useId();
  const textbox = useRef<HTMLInputElement>(null);
  const request = useRef<AbortController | null>(null);
  const [result, setResult] = useState<{
    input: ModelDiscoveryInput; loading?: boolean; ids?: string[]; error?: string;
  }>();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const current = result?.input === input ? result : undefined;
  const candidates = current?.ids?.filter(item => item.toLowerCase().includes(value.trim().toLowerCase())) ?? [];
  const expanded = open && current?.ids !== undefined;
  const selected = expanded && active >= 0 && active < candidates.length ? active : -1;

  useEffect(() => () => { request.current?.abort(); }, [input]);
  useEffect(() => {
    if (selected >= 0) document.getElementById(`${id}-option-${selected}`)?.scrollIntoView?.({ block: 'nearest' });
  }, [id, selected]);

  const fetchModels = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setResult({ input, loading: true });
    setOpen(false); setActive(-1);
    try {
      const next = await adapter.discoverModels(input, controller.signal);
      if (controller.signal.aborted || request.current !== controller) return;
      setResult({ input, ids: next.model_ids });
      setOpen(true);
      textbox.current?.focus();
    } catch (error) {
      if (controller.signal.aborted || request.current !== controller) return;
      setResult({ input, error: error instanceof Error ? error.message : '暂时无法获取模型列表。' });
    }
  };
  const choose = (model: string) => {
    onChange(model); setOpen(false); setActive(-1); textbox.current?.focus();
  };

  return <div className="model-id-field" onBlur={event => {
    if (!event.currentTarget.contains(event.relatedTarget as Node | null)) { setOpen(false); setActive(-1); }
  }}>
    <div className="model-id-heading"><label htmlFor={id}>Model ID</label>
      <button type="button" className="settings-header-action" disabled={!input.base_url.trim() || current?.loading}
        onClick={() => void fetchModels()}>{current?.loading ? <LoaderCircle size={13} /> : <RefreshCw size={13} />}{current?.loading ? '获取中…' : '获取模型'}</button>
    </div>
    <div className="model-id-control">
      <input ref={textbox} id={id} role="combobox" aria-autocomplete="list" autoComplete="off"
        aria-expanded={expanded} aria-controls={expanded ? `${id}-list` : undefined}
        aria-activedescendant={selected >= 0 ? `${id}-option-${selected}` : undefined}
        aria-describedby={`${id}-hint`} value={value} placeholder="输入 Model ID，或获取列表后搜索选择"
        onFocus={() => { setOpen(true); setActive(-1); }}
        onChange={event => { onChange(event.target.value); setOpen(true); setActive(-1); }}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing) return;
          if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && current?.ids) {
            event.preventDefault(); setOpen(true);
            setActive(candidates.length ? event.key === 'ArrowDown'
              ? (selected + 1) % candidates.length
              : (selected <= 0 ? candidates.length - 1 : selected - 1) : -1);
          } else if (event.key === 'Enter' && selected >= 0) {
            event.preventDefault(); choose(candidates[selected]);
          } else if (event.key === 'Escape' && expanded) {
            event.preventDefault(); event.stopPropagation(); setOpen(false); setActive(-1);
          }
        }} />
      {current?.ids && <button type="button" aria-label="展开模型列表" onClick={() => {
        textbox.current?.focus(); setOpen(true); setActive(-1);
      }}><ChevronDown size={14} /></button>}
    </div>
    {expanded && <div className="model-id-options" id={`${id}-list`} role="listbox" aria-label="模型候选列表">
      {candidates.map((model, index) => <div key={model} id={`${id}-option-${index}`} role="option"
        aria-selected={selected === index} className={selected === index ? 'is-active' : ''}
        onMouseEnter={() => setActive(index)} onMouseDown={event => event.preventDefault()}
        onClick={() => choose(model)}>{model}</div>)}
      {!candidates.length && <div className="model-id-empty">{current?.ids?.length ? '没有匹配的模型，可继续手动填写。' : '服务返回了空列表，可手动填写 Model ID。'}</div>}
    </div>}
    <small id={`${id}-hint`} role="status">{current?.error
      ? `${current.error} 仍可手动填写并保存。`
      : current?.ids ? `已获取 ${current.ids.length} 个模型；可搜索选择，也可填写列表外的 ID。`
        : '填写 Base URL 即可获取列表；没有密钥时匿名查询，也可直接手填。'}</small>
  </div>;
}
