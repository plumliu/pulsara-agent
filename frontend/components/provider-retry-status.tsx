import { useEffect, useState } from 'react';
import type { ProviderRetry } from '../lib/runtime-adapter';

export function retryStatusText(retry: ProviderRetry, now: number): string {
  const count = `（第 ${retry.retry_number}/${retry.max_retries} 次）`;
  if (retry.phase === 'reconnecting') {
    return `${retry.reason === 'tool_argument_regeneration' || retry.has_semantic_output ? '本次回复中断，正在重新生成' : '正在重新连接'}${count}`;
  }
  const seconds = Math.ceil(Math.max(0, retry.remaining_seconds - Math.max(0, now - retry.observedAt) / 1000));
  const reason = retry.reason === 'tool_argument_regeneration' ? '工具参数格式异常'
    : retry.reason === 'provider_recovery' ? '模型服务暂时不可用'
      : retry.has_semantic_output ? '本次回复中断' : '连接中断';
  return `${reason}，${seconds} 秒后重试${count}`;
}

export function ProviderRetryStatus({ retry }: { retry?: ProviderRetry | null }) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!retry || retry.phase !== 'waiting') return;
    const timer = setInterval(() => setNow(performance.now()), 200);
    return () => clearInterval(timer);
  }, [retry]);
  if (!retry) return null;
  return <div className="provider-retry-status" role="status">{retryStatusText(retry, Math.max(now, performance.now()))}</div>;
}
