import { createPortal } from 'react-dom';
import { Info } from 'lucide-react';
import { useEffect, useId, useState } from 'react';
import { usePromptHover } from '../lib/prompt-hover';
import type { TurnInterruptionNotice } from '../lib/runtime-adapter';

const reasons: Record<string, string> = {
  USER_STOPPED: '你已停止本轮回复。', SESSION_CLOSED: '会话运行时已关闭。',
  HOST_TAKEOVER: '原运行时已被替换。', PROVIDER_REQUEST_FAILED: '模型服务请求失败。',
  MODEL_OUTPUT_TOKEN_LIMIT_REACHED: '模型输出达到额度上限。',
  MODEL_OUTPUT_CONTEXT_LIMIT_REACHED: '模型上下文达到上限。',
  MODEL_OUTPUT_CONTENT_FILTERED: '模型输出受到内容过滤。',
  MODEL_OUTPUT_INCOMPLETE: '模型未能完整返回本轮输出。',
  PROVIDER_INPUT_PLAN_CONFLICT: '模型输入连续性检查未通过。',
  PROVIDER_INPUT_RESOURCE_EXHAUSTED: '模型输入超过当前资源边界。',
  PLAN_CONTINUATION_NOT_BOUND: '计划已接受，但本轮未能继续。',
  PROVIDER_OUTPUT_RESOURCE_EXHAUSTED: '模型输出超过当前资源边界。',
  HOOK_SESSION_START_BLOCKED: 'Hook 阻止了本轮启动。',
  HOOK_COMPACTION_BLOCKED: 'Hook 阻止了本次上下文压缩。',
};

export function TurnInterruptionHistoryNotice({notice}: {notice: TurnInterruptionNotice}) {
  const { trigger: triggerRef, details: detailsRef, position, show, hide, keep, dismiss } = usePromptHover<HTMLButtonElement, HTMLDivElement>(360);
  const [pinned, setPinned] = useState(false);
  const id = useId();
  useEffect(() => {
    const clear = (event: KeyboardEvent) => { if (event.key === 'Escape') setPinned(false); };
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !triggerRef.current?.contains(event.target) && !detailsRef.current?.contains(event.target)) setPinned(false);
    };
    document.addEventListener('keydown', clear); document.addEventListener('pointerdown', outside);
    return () => {document.removeEventListener('keydown', clear); document.removeEventListener('pointerdown', outside);};
  }, [triggerRef, detailsRef]);
  return <div className="conversation-interruption" data-interruption-owner={`${notice.ownerKind}:${notice.ownerId}`}>
    <span>本轮回复已中断。</span>
    <button ref={triggerRef} type="button" aria-label="查看中断详情" aria-describedby={position ? id : undefined}
      aria-expanded={Boolean(position)} onMouseEnter={show} onFocus={show}
      onMouseLeave={() => {if (!pinned) hide();}} onBlur={() => {if (!pinned) hide();}}
      onClick={() => {if (pinned) {setPinned(false); dismiss();} else {setPinned(true); show();}}}>
      <Info size={14} />
    </button>
    {position && createPortal(<div ref={detailsRef} id={id} role="tooltip" className="interruption-details"
      onMouseEnter={keep} onMouseLeave={() => {if (!pinned) hide();}}
      style={{position:'fixed',left:position.left,top:position.top,
        maxHeight:Math.max(80, position.above?position.top-16:window.innerHeight-position.top-16),
        overflowY:'auto',transform:position.above?'translateY(calc(-100% - 8px))':'translateY(8px)'}}>
      <p>{reasons[notice.reason] ?? '未能确定具体原因。'}</p>
      {notice.publicDetail && <p>{notice.publicDetail}</p>}
      <time dateTime={notice.terminalAtUtc}>{new Date(notice.terminalAtUtc).toLocaleString('zh-CN')}</time>
    </div>,document.body)}
  </div>;
}
