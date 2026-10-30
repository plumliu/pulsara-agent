import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ProviderRetryStatus, retryStatusText } from '../../frontend/components/provider-retry-status';
import type { ProviderRetry } from '../../frontend/lib/runtime-adapter';
const retry: ProviderRetry = { session_id:'session:a',turn_id:'turn:a',resolved_model_call_id:'call:a',draft_id:'draft:a',attempt:3,retry_number:3,max_retries:5,phase:'waiting',reason:'connection_recovery',remaining_seconds:8.3,has_semantic_output:false,observedAt:1000 };
afterEach(()=>{cleanup();vi.useRealTimers();vi.restoreAllMocks();});
it('counts down from receipt with a monotonic local clock and rounds up',()=>{
 expect(retryStatusText(retry,1000)).toBe('连接中断，9 秒后重试（第 3/5 次）');
 expect(retryStatusText(retry,1300)).toBe('连接中断，8 秒后重试（第 3/5 次）');
 expect(retryStatusText(retry,12000)).toBe('连接中断，0 秒后重试（第 3/5 次）');
 expect(retryStatusText({...retry,phase:'reconnecting'},999999)).toBe('正在重新连接（第 3/5 次）');
 expect(retryStatusText({...retry,phase:'reconnecting',has_semantic_output:true},999999)).toBe('本次回复中断，正在重新生成（第 3/5 次）');
 expect(retryStatusText({...retry,reason:'provider_recovery'},1000)).toContain('模型服务暂时不可用');
 expect(retryStatusText({...retry,reason:'tool_argument_regeneration'},1000)).toContain('工具参数格式异常');
});
it('clears explicit null, restores the current fact on remount, and adds no controls',()=>{
 vi.useFakeTimers(); let now=1000;vi.spyOn(performance,'now').mockImplementation(()=>now);
 const view=render(<ProviderRetryStatus retry={retry}/>);expect(screen.getByRole('status').textContent).toContain('9 秒');
 now=3400;act(()=>vi.advanceTimersByTime(400));expect(screen.getByRole('status').textContent).toContain('6 秒');
 view.rerender(<ProviderRetryStatus retry={null}/>);expect(screen.queryByRole('status')).toBeNull();
 view.rerender(<ProviderRetryStatus retry={{...retry,phase:'reconnecting'}}/>);expect(screen.getByRole('status').textContent).toContain('正在重新连接');
 expect(screen.queryByRole('button')).toBeNull();
 view.unmount();render(<ProviderRetryStatus retry={{...retry,remaining_seconds:2,observedAt:now}}/>);expect(screen.getByRole('status').textContent).toContain('2 秒');
});
