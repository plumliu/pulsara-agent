import { describe, expect, it } from 'vitest';
import type { ToolTrace } from '../../frontend/lib/pulsara-types';
import { builtinToolSummary } from '../../frontend/lib/builtin-tool-summary';

function trace(name: string, result: unknown, args: unknown = {}, extra: Partial<ToolTrace> = {}): ToolTrace {
  return { id: 'call', kind: 'tool', toolName: name, title: name, subtitle: '已完成',
    status: 'completed', resultState: 'SUCCESS', argumentsJson: JSON.stringify(args),
    resultText: JSON.stringify(result), ...extra };
}

// One real result shape per non-MCP builtin, from builtin_catalog and its owner.
// Assertions protect product meaning, especially acknowledgements of unfinished work.
describe('builtin tool summaries', () => {
  it('uses accepted control facts when the displayed text does not carry lifecycle metadata', () => {
    const value = trace('exec_process', 'native log preview', {}, {
      resultFacts: { status: 'running', exit_code: -1, yielded_to_background: true },
    });
    expect(builtinToolSummary(value)?.subtitle).toContain('命令仍在后台运行');
  });
  it.each([
    ['read_file', {target: {kind:'PATH',path:'src/main.py'}, total_lines:42, next:{tool:'read_file',arguments:{}}}, {}, '读取文件', '共 42 行，本次仅返回部分内容'],
    ['view_image', {}, { target: {kind: 'IMAGE_REF', image_ref: 'opaque-private-ref'} }, '查看图片', '对话中已保存的图片'],
    ['search_content', {matches: [{line:1},{line:2},{line:3}], next:null}, {pattern:'TODO',target:{kind:'PATH',path:'src'}}, '搜索内容', '本次找到 3 行匹配'],
    ['find_files', {targets:[{kind:'PATH',path:'src/a.py'}],next:{tool:'find_files',arguments:{}}}, {glob: '*.py', target:{kind:'PATH', path: 'src'}}, '查找文件', '本次找到 1 个文件，可继续搜索'],
    ['search_content', {counts:[{target:{kind:'PATH',path:'z.py'},count:2}],next:null}, {pattern:'TODO',output_mode:'count'}, '搜索内容', '本次列出 1 个文件的匹配行数'],
    ['search_content', {output_mode:'files_only',targets:[{kind:'PATH',path:'z.py'}],next:null}, {pattern:'TODO',output_mode:'files_only'}, '搜索内容', '本次找到 1 个文件'],
    ['edit_file', { path: 'main.py', diff: 'RAW DIFF' }, {}, '修改文件', '已修改文件'],
    ['write_file', { bytes_written: 123, path: 'new.txt' }, {}, '写入文件', '已写入 123 字节'],
    ['read_file', {content: 'RAW OUTPUT', next: {tool: 'read_file', arguments: {target: {kind: 'SESSION_ENTRY', entry_id: 'private-id'}}}, source_coverage: 'RETAINED_SNAPSHOT'}, {target: {kind: 'SESSION_ENTRY', entry_id: 'private-id'}}, '读取内容', '已读取保留的输出，还有后续内容 · 仅保留了部分原始输出'],
    ['exec_process', { status: 'running', exit_code: -1, yielded_to_background: true, output: 'RAW STDOUT' }, {}, '运行命令', '命令仍在后台运行'],
    ['manage_process', { status: 'success', exit_code: 0, output: 'RAW OUTPUT' }, { action: 'poll' }, '查看命令状态', '命令执行完成'],
    ['todo', { status: 'UPDATED', counts: { total: 5, completed: 2 } }, {}, '更新工作清单', '2/5 项已完成'],
    ['enter_plan', { plan_control: 'ENTERED_PLAN' }, {}, '开始规划', '已进入规划模式'],
    ['ask_plan_question', { plan_control: 'QUESTION_ANSWERED' }, {}, '确认方案细节', '已记录你的选择'],
    ['exit_plan', { plan_control: 'DRAFT_SUBMITTED_FOR_REVIEW' }, {}, '提交方案', '方案已提交，等待你的确认'],
    ['search_memory', { memories: [{ statement: 'RAW MEMORY' }] }, { query: '偏好' }, '搜索记忆', '本次找到 1 条相关记忆'],
    ['get_memory', { statement: 'RAW MEMORY' }, { memory_id: 'private-id' }, '读取记忆', '已读取记忆'],
    ['remember', { status: 'SAVED', memory_id: 'private-id' }, {}, '提交记忆', '已提交记忆'],
    ['mark_memory_relation', { status: 'SAVED', relation_kind: 'SUPERSEDES', source_memory_id: 'private-id', target_memory_id: 'private-id' }, {}, '标记记忆关系', '已标记取代关系'],
    ['mark_memory_relation', { status: 'SAVED', relation_kind: 'CONTRADICTS' }, {}, '标记记忆关系', '已标记冲突关系'],
    ['mark_memory_relation', { status: 'ALREADY_PRESENT', relation_kind: 'SUPERSEDES' }, {}, '标记记忆关系', '取代关系已存在'],
    ['manage_capability', { status: 'APPLIED', adoption: { status: 'PARTIAL' } }, {}, '管理扩展能力', '配置变更已保存，部分配置尚未生效'],
    ['reload_capabilities', { status: 'PARTIAL' }, {}, '刷新扩展能力', '部分配置未能刷新'],
    ['reload_capabilities', { status: 'RELOADED' }, {}, '刷新扩展能力', '已刷新技能、连接与自动操作'],
  ])('%s explains the observed result without exposing raw payloads', (name, result, args, title, detail) => {
    const summary = builtinToolSummary(trace(name as string, result, args));
    expect(summary?.title).toBe(title);
    expect(summary?.subtitle).toContain(detail);
    expect(summary?.subtitle).not.toMatch(/RAW |private-id|opaque-private-ref|proposed_for_review|PARTIAL/);
  });

  it.each([
    ['exec_process', { status: 'error', exit_code: 2 }, '命令未成功，退出码 2'],
    ['exec_process', { status: 'error', exit_code: -1 }, '命令执行失败'],
    ['exec_process', { status: 'timeout', timed_out: true }, '命令已超时'],
    ['exec_process', { status: 'blocked', reason: 'PROCESS_CAPACITY_EXHAUSTED', process_id: null }, '执行容量已满，命令未启动'],
    ['manage_process', { status: 'killed' }, '命令已停止'],
    ['manage_process', { status: 'blocked', exit_code: -1 }, '命令未获准执行'],
    ['view_image', { error: 'MODEL_IMAGE_INPUT_UNSUPPORTED' }, '当前模型不支持查看图片'],
    ['edit_file', { error: 'CONTENT_REVISION_MISMATCH' }, '文件已变化，本次修改未应用'],
    ['manage_capability', { status: 'CONFLICT' }, '配置已变化，本次变更未应用'],
  ])('%s describes a failed operation without claiming success', (name, result, expected) => {
    expect(builtinToolSummary(trace(name as string, result, {}, { status: 'failed', resultState: 'APPLICATION_ERROR' }))?.subtitle).toBe(expected);
  });

  it('does not expose the terminal pending-exit placeholder as a failed exit code', () => {
    expect(builtinToolSummary(trace('exec_process', {
      status: 'running', exit_code: -1, yielded_to_background: true,
    }))?.subtitle).toBe('命令仍在后台运行');
    expect(builtinToolSummary(trace('exec_process', {
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
    expect(builtinToolSummary(trace('manage_process', {
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
    const summary = builtinToolSummary(trace('exec_process', {}, {}, {
      argumentsJson: '{"internal_owner":"private', resultText: 'RAW HEAD TAIL OUTPUT',
    }));
    expect(summary?.subtitle).toBe('已执行命令');
  });

  it('summarizes the unified queries without exposing internal configuration', () => {
    expect(builtinToolSummary(trace('query_capabilities', { items: [] }))?.title).toBe('查看能力');
    expect(builtinToolSummary(trace('query_capabilities', { target: { kind: 'PLUGIN' } }, { target: { kind: 'PLUGIN', scope: 'USER', plugin_id: 'fixture' } }))?.title).toBe('查看能力详情');
  });

  it.each(['mcp__server__read_file', 'use_new_mcp_tool', 'read_mcp_resource'])('leaves %s to the external tool presentation', (name) => {
    expect(builtinToolSummary(trace(name, { content: 'external' }))).toBeUndefined();
  });
});
