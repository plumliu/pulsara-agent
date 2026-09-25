import { describe, expect, it } from 'vitest';
import type { ToolTrace } from './pulsara-types';
import { builtinToolSummary } from './builtin-tool-summary';

function trace(name: string, result: unknown, args: unknown = {}, extra: Partial<ToolTrace> = {}): ToolTrace {
  return { id: 'call', kind: 'artifact', toolName: name, title: name, subtitle: '已完成',
    status: 'completed', resultState: 'SUCCESS', argumentsJson: JSON.stringify(args),
    resultText: JSON.stringify(result), ...extra };
}

// One real result shape per non-MCP builtin, from builtin_catalog and its owner.
// Assertions protect product meaning, especially acknowledgements of unfinished work.
describe('builtin tool summaries', () => {
  it.each([
    ['read_file', { path: 'src/main.py', total_lines: 42, truncated: true }, {}, '读取文件', '共 42 行，本次仅返回部分内容'],
    ['view_image', {}, { image_ref: 'opaque-private-ref' }, '查看图片', '对话中已保存的图片'],
    ['search_files', { total_count: 3, matches: [], truncated: false }, { pattern: 'TODO', path: 'src' }, '搜索文件', '找到 3 处匹配'],
    ['edit_file', { path: 'main.py', diff: 'RAW DIFF' }, {}, '修改文件', '已修改文件'],
    ['write_file', { bytes_written: 123, path: 'new.txt' }, {}, '写入文件', '已写入 123 字节'],
    ['artifact_read', { returned_chars: 300, has_more: true, source_coverage: 'RETAINED_SNAPSHOT', text: 'RAW OUTPUT' }, {}, '读取完整输出', '已读取 300 个字符，还有后续内容 · 仅保留了部分原始输出'],
    ['terminal', { status: 'running', exit_code: -1, yielded_to_background: true, output: 'RAW STDOUT' }, {}, '运行命令', '命令仍在后台运行'],
    ['terminal_process', { status: 'success', exit_code: 0, output: 'RAW OUTPUT' }, { action: 'poll' }, '查看命令状态', '命令执行完成'],
    ['terminal_monitor', { status: 'REGISTERED' }, { action: 'register' }, '关注命令进展', '已开启进展通知'],
    ['todo', { status: 'UPDATED', counts: { total: 5, completed: 2 } }, {}, '更新工作清单', '2/5 项已完成'],
    ['spawn_agent', { status: 'pending_start', task_id: 'private-id' }, { task_name: 'reviewer' }, '创建子任务', '子任务等待开始'],
    ['create_agent_tasks', { tasks: [{ status: 'pending_start' }, { status: 'active' }] }, {}, '创建子任务', '已创建 2 个子任务'],
    ['list_agents', { tasks: [{ status: 'active' }], has_more: true }, {}, '查看子任务', '本次列出 1 个子任务，还有后续任务'],
    ['wait_agent', { outcome: 'timeout', pending_task_ids: ['private-id'] }, {}, '等待子任务', '已等待子任务'],
    ['stop_agent', { status: 'cancelled' }, {}, '停止子任务', '子任务已取消'],
    ['send_agent_message', { status: 'queued' }, {}, '联系子任务', '已发送补充信息'],
    ['report_agent_result', { status: 'accepted' }, {}, '提交子任务结果', '子任务结果已接收'],
    ['enter_plan', { plan_control: 'ENTERED_PLAN' }, {}, '开始规划', '已进入规划模式'],
    ['ask_plan_question', { plan_control: 'QUESTION_ANSWERED' }, {}, '确认方案细节', '已记录你的选择'],
    ['exit_plan', { plan_control: 'DRAFT_SUBMITTED_FOR_REVIEW' }, {}, '提交方案', '方案已提交，等待你的确认'],
    ['memory_search', { memories: [{ statement: 'RAW MEMORY' }] }, { query: '偏好' }, '搜索记忆', '本次找到 1 条相关记忆'],
    ['memory_get', { statement: 'RAW MEMORY' }, { memory_id: 'private-id' }, '读取记忆', '已读取记忆'],
    ['memory_explain', { statement: 'RAW MEMORY', decision: {} }, {}, '查看记忆来源', '已读取记忆的来源与审核记录'],
    ['remember', { status: 'SAVED', memory_id: 'private-id' }, {}, '提交记忆', '已提交记忆'],
    ['mark_memory_relation', { status: 'SAVED', relation_kind: 'SUPERSEDES', source_memory_id: 'private-id', target_memory_id: 'private-id' }, {}, '标记记忆关系', '已标记取代关系'],
    ['mark_memory_relation', { status: 'SAVED', relation_kind: 'CONTRADICTS' }, {}, '标记记忆关系', '已标记冲突关系'],
    ['mark_memory_relation', { status: 'ALREADY_PRESENT', relation_kind: 'SUPERSEDES' }, {}, '标记记忆关系', '取代关系已存在'],
    ['manage_capability', { status: 'APPLIED', adoption: { status: 'PARTIAL' } }, {}, '管理扩展能力', '配置变更已保存，部分配置尚未生效'],
    ['reload_capabilities', { status: 'PARTIAL' }, {}, '刷新扩展能力', '部分配置未能刷新'],
    ['reload_hooks', { status: 'RELOADED' }, {}, '刷新自动操作', '已刷新自动操作配置'],
  ])('%s explains the observed result without exposing raw payloads', (name, result, args, title, detail) => {
    const summary = builtinToolSummary(trace(name as string, result, args));
    expect(summary?.title).toBe(title);
    expect(summary?.subtitle).toContain(detail);
    expect(summary?.subtitle).not.toMatch(/RAW |private-id|opaque-private-ref|proposed_for_review|PARTIAL/);
  });

  it.each([
    ['terminal', { status: 'error', exit_code: 2 }, '命令未成功，退出码 2'],
    ['terminal', { status: 'error', exit_code: -1 }, '命令执行失败'],
    ['terminal', { status: 'timeout', timed_out: true }, '命令已超时'],
    ['terminal', { status: 'blocked', reason: 'PROCESS_CAPACITY_EXHAUSTED', process_id: null }, '执行容量已满，命令未启动'],
    ['terminal_process', { status: 'killed' }, '命令已停止'],
    ['terminal_process', { status: 'blocked', exit_code: -1 }, '命令未获准执行'],
    ['view_image', { error: 'MODEL_IMAGE_INPUT_UNSUPPORTED' }, '当前模型不支持查看图片'],
    ['edit_file', { error: 'CONTENT_REVISION_MISMATCH' }, '文件已变化，本次修改未应用'],
    ['report_agent_result', { status: 'not_accepted' }, '子任务结果未被接收'],
    ['manage_capability', { status: 'CONFLICT' }, '配置已变化，本次变更未应用'],
  ])('%s describes a failed operation without claiming success', (name, result, expected) => {
    expect(builtinToolSummary(trace(name as string, result, {}, { status: 'failed', resultState: 'APPLICATION_ERROR' }))?.subtitle).toBe(expected);
  });

  it('does not expose the terminal pending-exit placeholder as a failed exit code', () => {
    expect(builtinToolSummary(trace('terminal', {
      status: 'running', exit_code: -1, yielded_to_background: true,
    }))?.subtitle).toBe('命令仍在后台运行');
    expect(builtinToolSummary(trace('terminal', {
      status: 'unknown', exit_code: -1,
    }))?.subtitle).toBe('命令退出状态尚未确定');
  });

  it.each([
    ['poll', 'running', -1, '命令仍在运行'],
    ['wait', 'running', -1, '等待结束，命令仍在运行'],
    ['write', 'running', -1, '已发送输入 · 命令仍在运行'],
    ['wait', 'success', 0, '命令执行完成'],
    ['poll', 'error', 7, '命令未成功，退出码 7'],
    ['wait', 'timeout', 124, '命令已超时'],
    ['kill', 'killed', -15, '命令已停止'],
  ])('describes the completed %s action for a %s process', (action, status, exitCode, expected) => {
    expect(builtinToolSummary(trace('terminal_process', {
      status, exit_code: exitCode, process_id: 'private-process-id',
    }, { action }))?.subtitle).toBe(expected);
  });

  it('keeps unfinished calls and unconfirmed historical results distinct', () => {
    expect(builtinToolSummary(trace('remember', undefined, {}, { status: 'running' }))?.subtitle).toBe('正在提交记忆');
    expect(builtinToolSummary(trace('mark_memory_relation', undefined, { relation_kind: 'CONTRADICTS' }, { status: 'running' }))?.subtitle).toBe('正在标记冲突关系');
    expect(builtinToolSummary(trace('mark_memory_relation', undefined, { relation_kind: 'SUPERSEDES' }, { status: 'running' }))?.subtitle).toBe('正在标记取代关系');
    expect(builtinToolSummary(trace('write_file', undefined, {}, { status: 'cancelled', resultState: undefined }))?.subtitle).toBe('未记录到操作结果');
    expect(builtinToolSummary(trace('write_file', undefined, {}, { status: 'cancelled', resultState: 'CANCELLED_BEFORE_DISPATCH' }))?.subtitle).toBe('操作已取消');
  });

  it('does not show malformed argument or result bodies in the default summary', () => {
    const summary = builtinToolSummary(trace('terminal', {}, {}, {
      argumentsJson: '{"internal_owner":"private', resultText: 'RAW HEAD TAIL OUTPUT',
    }));
    expect(summary?.subtitle).toBe('已执行命令');
  });

  it.each(['mcp__server__read_file', 'list_mcp_servers', 'inspect_new_mcp_tool', 'use_new_mcp_tool',
    'list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource', 'list_mcp_prompts', 'get_mcp_prompt'])('leaves %s to the external tool presentation', (name) => {
    expect(builtinToolSummary(trace(name, { content: 'external' }))).toBeUndefined();
  });
});
