import {Plus, Trash2} from 'lucide-react';
import {useState} from 'react';
import events from '../lib/hook-form-events.json';
import {editHookField, moveHookGroup, readHookForm} from '../lib/hook-config-form';
import type {JSONPath} from 'jsonc-parser';

const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const textValue = (value: unknown) => typeof value === 'string' ? value : '';

export function HookConfigForm({text, disabled, onChange}: {text: string; disabled: boolean; onChange: (text: string) => void}) {
  const form = readHookForm(text);
  const [error, setError] = useState('');
  const change = (operation: () => string) => {
    try {onChange(operation()); setError('');}
    catch {setError('这项配置无法用表单修改，请返回来源行，通过 JSON 入口调整。');}
  };
  const edit = (path: JSONPath, value: unknown) => change(() => editHookField(text, path, value));
  if (form.error) return <p className="capability-inline-error" role="alert">{form.error}</p>;
  return <>
    {error && <p className="capability-inline-error" role="alert">{error}</p>}
    {form.unrepresented && <p className="capability-detail-note">部分条目无法用表单展示，原文会保留，可通过 JSON 入口修改。</p>}
    {form.groups.map((group, ordinal) => {
      const event = events.find(item => item.value === group.event);
      const base: JSONPath = ['hooks', group.event, group.index];
      return <div className="skill-import-candidate" key={`${group.event}:${group.index}`}>
        <div className="capability-form-grid">
          <label className="capability-field capability-field--wide"><span>触发事件</span><select disabled={disabled} value={group.event} onChange={e => change(() => moveHookGroup(text, group, e.target.value))}>
            {!event && <option value={group.event}>{group.event}（现有事件）</option>}
            {events.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select></label>
          {event?.matcher !== false && <details className="capability-field capability-field--wide"><summary>匹配条件（可选）</summary><label className="capability-field"><input aria-label="匹配条件" disabled={disabled} value={textValue(group.value.matcher)} placeholder="留空匹配全部" onChange={e => edit([...base, 'matcher'], e.target.value || undefined)}/><small>支持正则表达式；工具事件可填写工具名，例如 read_file。</small></label></details>}
        </div>
        {group.handlers.map((value, index) => {
          const handler = object(value);
          const path: JSONPath = [...base, 'hooks', index];
          if (handler.type !== 'command') return <p className="capability-detail-note" key={index}>命令 {index + 1} 使用表单未支持的类型，原文会保留。</p>;
          return <div className="hook-command-fields" key={index}>
            <div className="capability-plugin-summary"><span>{group.handlers.length > 1 ? `命令 ${index + 1}` : '执行命令'}</span>{group.handlers.length > 1 && <div className="capability-detail-actions"><button className="danger-ghost" disabled={disabled} aria-label={`删除规则 ${ordinal + 1} 的命令 ${index + 1}`} onClick={() => edit(path, undefined)}><Trash2 size={13}/>删除命令</button></div>}</div>
            <label className="capability-field"><textarea aria-label={`规则 ${ordinal + 1} 的命令 ${index + 1}`} required disabled={disabled} value={textValue(handler.command)} placeholder="例如 python /path/to/hook.py" spellCheck={false} onChange={e => edit([...path, 'command'], e.target.value)}/></label>
            <details className="capability-field"><summary>高级选项</summary><div className="capability-form-grid">
              <label className="capability-field"><span>超时（秒）</span><input type="number" min={1} max={group.event === 'SessionEnd' ? 3 : 600} step={1} disabled={disabled} value={typeof handler.timeout === 'number' ? handler.timeout : ''} placeholder={group.event === 'SessionEnd' ? '默认 1 秒' : '默认 600 秒'} onChange={e => edit([...path, 'timeout'], e.target.value === '' ? undefined : Number(e.target.value))}/></label>
              <label className="capability-field"><span>执行方式</span><select disabled={disabled || group.event === 'SessionEnd'} value={handler.async === true && group.event !== 'SessionEnd' ? 'async' : 'sync'} onChange={e => edit([...path, 'async'], e.target.value === 'async')}><option value="sync">同步</option><option value="async">异步</option></select>{group.event === 'SessionEnd' && <small>会话结束时按同步方式执行。</small>}</label>
              <label className="capability-field capability-field--wide"><span>Windows 命令（可选）</span><input disabled={disabled} value={textValue(handler.commandWindows)} onChange={e => edit([...path, 'commandWindows'], e.target.value || undefined)}/></label>
              <label className="capability-field capability-field--wide"><span>状态说明（可选）</span><input disabled={disabled} value={textValue(handler.statusMessage)} onChange={e => edit([...path, 'statusMessage'], e.target.value || undefined)}/></label>
              {(event?.context || handler.additionalContextLimit !== undefined) && <label className="capability-field capability-field--wide"><span>附加上下文上限（tokens）</span><input type="number" min={0} step={1} disabled={disabled} value={typeof handler.additionalContextLimit === 'number' ? handler.additionalContextLimit : ''} placeholder="默认 2500" onChange={e => edit([...path, 'additionalContextLimit'], e.target.value === '' ? undefined : Number(e.target.value))}/>{!event?.context && <small>此事件不使用附加上下文限制，现有值会保留。</small>}</label>}
            </div></details>
          </div>;
        })}
        <button className="secondary-ghost" disabled={disabled} onClick={() => edit([...base, 'hooks', -1], {type: 'command', command: ''})}><Plus size={13}/>添加命令</button>
      </div>;
    })}
    <p className="capability-detail-note">保存后需另行审阅信任；规则内的命令共享触发事件与匹配条件。</p>
  </>;
}
