import type { ToolTrace } from './pulsara-types';

// Presentation only. Names and result fields follow builtin_catalog and each
// production tool owner; MCP direct and meta tools keep their existing views.
const labels: Record<string, [title: string, pending: string, completed: string]> = {
  open_in_pulsara: ['展示内容', '正在准备展示请求', '已请求展示'],
  read_file: ['读取文件', '正在读取文件', '已读取文件'],
  view_image: ['查看图片', '正在读取图片', '已读取图片'],
  search_content: ['搜索内容', '正在搜索内容', '搜索已结束'],
  find_files: ['查找文件', '正在查找文件', '查找已结束'],
  edit_file: ['修改文件', '正在修改文件', '已修改文件'],
  write_file: ['写入文件', '正在写入文件', '已写入文件'],
  read_artifact: ['读取完整输出', '正在读取之前保留的输出', '已读取保留的输出'],
  export_artifact: ['导出完整输出', '正在导出保留的输出', '已导出保留的输出'],
  exec_process: ['运行命令', '命令正在执行', '已执行命令'],
  query_processes: ['查看命令', '正在读取命令状态', '已读取命令状态'],
  manage_process: ['管理命令', '正在处理命令进程', '已查看命令状态'],
  query_monitors: ['查看进展通知', '正在读取通知设置', '已读取通知设置'],
  manage_monitor: ['关注命令进展', '正在设置进展通知', '已处理通知设置'],
  exec_cell: ['运行脚本', '脚本正在执行', '已观察脚本执行'],
  query_cells: ['查看脚本', '正在读取脚本状态与源码', '已读取脚本信息'],
  manage_cell: ['管理脚本', '正在处理脚本', '已处理脚本'],
  query_agents: ['查看任务', '正在读取任务状态', '已读取任务状态'],
  query_schedules: ['查看定时计划', '正在读取定时计划', '已读取定时计划'],
  manage_schedule: ['管理定时计划', '正在处理定时计划', '已处理定时计划'],
  todo: ['更新工作清单', '正在更新工作清单', '已更新工作清单'],
  enter_plan: ['开始规划', '正在进入规划模式', '已进入规划模式'],
  ask_plan_question: ['确认方案细节', '等待你的选择', '已记录你的选择'],
  exit_plan: ['提交方案', '正在提交方案', '方案已提交，等待你的确认'],
  search_memory: ['搜索记忆', '正在查找相关记忆', '记忆搜索已结束'],
  get_memory: ['读取记忆', '正在读取已保存的记忆', '已读取记忆'],
  remember: ['提交记忆', '正在提交记忆', '已提交记忆'],
  mark_memory_relation: ['标记记忆关系', '正在标记记忆关系', '已处理记忆关系'],
  query_capabilities: ['查看能力', '正在查看能力及来源', '已读取能力信息'],
  manage_capability: ['管理扩展能力', '正在处理连接或插件配置', '已处理配置请求'],
  reload_capabilities: ['刷新扩展能力', '正在刷新技能、连接与自动操作', '已刷新技能、连接与自动操作'],
};

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown> : {};
}
function parse(text?: string): Record<string, unknown> {
  try { return object(JSON.parse(text ?? '')); } catch { return {}; }
}
function text(value: unknown): string {
  return typeof value === 'string' ? value : '';
}
function number(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}
function count(value: unknown): number | undefined {
  return Array.isArray(value) ? value.length : undefined;
}
function compact(value: string): string {
  const line = value.replace(/\s+/g, ' ').trim();
  return line.length > 120 ? `${line.slice(0, 120)}…` : line;
}


const processActions: Record<string, string> = {
  poll: '查看命令状态', wait: '等待命令结束',
  write: '向命令输入内容', submit: '向命令提交输入', close_stdin: '结束命令输入', kill: '停止命令',
};
const monitorActions: Record<string, string> = {
  register: '关注命令进展', cancel: '取消进展通知',
};

function runningProcessDetail(name: string, action: string): string {
  if (name === 'exec_process') return '命令仍在后台运行';
  return ({
    poll: '命令仍在运行',
    wait: '等待结束，命令仍在运行',
    write: '已发送输入 · 命令仍在运行',
    submit: '已发送输入 · 命令仍在运行',
    close_stdin: '已结束输入 · 命令仍在运行',
    kill: '已请求停止，命令仍在运行',
  } as Record<string, string>)[action] ?? '命令仍在运行';
}

function failure(trace: ToolTrace, result: Record<string, unknown>): string {
  const errors: Record<string, string> = {
    FILE_NOT_FOUND: '未找到文件',
    FILE_ALREADY_EXISTS: '文件已存在，未覆盖原文件',
    CONTENT_REVISION_MISMATCH: '文件已变化，本次修改未应用',
    READ_OBSERVATION_REQUIRED: '尚未读取文件，本次修改未应用',
    READ_OUTPUT_TOO_LARGE: '文件内容超出单次读取范围',
    SEARCH_ROOT_TOO_BROAD: '搜索范围过大，本次搜索未执行',
    UNSUPPORTED_BINARY_FILE: '该文件不是可读取的文本文件',
    UNSUPPORTED_TEXT_ENCODING: '暂不支持该文件的文字编码',
    NOT_A_REGULAR_FILE: '所选路径不是普通文件',
    NO_OP: '内容没有变化，无需修改',
    MODEL_IMAGE_INPUT_UNSUPPORTED: '当前模型不支持查看图片',
    IMAGE_REFERENCE_UNAVAILABLE: '这张历史图片无法读取',
    IMAGE_RESOURCE_EXCEEDED: '图片超出可读取的大小或资源限制',
    IMAGE_PATH_NOT_REGULAR: '所选路径不是图片文件',
    IMAGE_READ_FAILED: '无法读取图片文件',
    IMAGE_DECODE_FAILED: '图片无法解码，文件可能已损坏',
    IMAGE_FORMAT_UNSUPPORTED: '暂不支持这种图片格式',
    IMAGE_VALIDATION_DEADLINE_EXPIRED: '图片检查超时',
    VISUALIZATION_PATH_INVALID: '可视化文件路径无效',
  };
  const states: Record<string, string> = {
    PERMISSION_DENIED: '未获授权，未执行操作', INVALID_ARGUMENTS: '参数不正确，未执行操作',
    TOOL_UNAVAILABLE: '工具当前不可用', RESOURCE_EXHAUSTED: '资源不足，操作未完成',
    SYSTEM_ERROR: '本地服务出错，操作未完成',
  };
  if (trace.toolName === 'exec_process' || trace.toolName === 'manage_process') {
    const process = result;
    const status = text(process.status).toLowerCase();
    if (process.timed_out === true || process.status === 'timeout') return '命令已超时';
    if (status === 'killed') return '命令已停止';
    if (status === 'blocked') return result.reason === 'PROCESS_CAPACITY_EXHAUSTED' ? '执行容量已满，命令未启动' : '命令未获准执行';
    if (status === 'running') return runningProcessDetail(trace.toolName, text(parse(trace.argumentsJson).action));
    const exitCode = number(process.exit_code);
    if (exitCode === -1) return status === 'error' ? '命令执行失败' : '命令退出状态尚未确定';
    if (exitCode !== undefined) return `命令未成功，退出码 ${exitCode}`;
  }
  if (trace.toolName === 'manage_capability') {
    if (result.status === 'CONFLICT') return '配置已变化，本次变更未应用';
    if (result.status === 'REJECTED') return '配置变更未被接受';
  }
  return errors[text(result.error)] ?? states[trace.resultState ?? ''] ?? '操作失败';
}

export function builtinToolSummary(trace: ToolTrace): { title: string; subtitle: string } | undefined {
  if (trace.toolName === 'open_in_pulsara' && trace.status === 'completed') {
    const result = parse(trace.resultText);
    const accepted = count(result.targets) ?? 0, rejected = count(result.errors) ?? 0;
    return { title: accepted ? '请求展示' : '目标未通过检查', subtitle: accepted ? `${accepted} 个目标${rejected ? `，${rejected} 项未通过检查` : ''}` : `${rejected} 项未通过检查` };
  }

  const name = trace.toolName ?? '';
  const label = labels[name];
  if (!label) return undefined;
  const args = parse(trace.argumentsJson);
  const result = parse(trace.resultText);
  const action = text(args.action);
  const title = (name === 'manage_process' ? processActions[action]
    : name === 'manage_monitor' ? monitorActions[action]
    : name === 'manage_cell' ? ({ wait: '等待脚本输出', stop: '停止脚本' } as Record<string, string>)[action]
    : name === 'query_capabilities' && Object.hasOwn(args, 'target') ? '查看能力详情' : undefined) ?? label[0];
  let target = '';
  if (['read_file', 'view_image', 'edit_file', 'write_file', 'search_content', 'find_files'].includes(name)) {
    target = text(record(result.target).path) || text(record(args.target).path);
    if (name === 'view_image' && record(args.target).kind === 'IMAGE_REF') target = '对话中已保存的图片';
    if (name === 'search_content' || name === 'find_files') target = [text(name === 'find_files' ? args.glob : args.pattern), target].filter(Boolean).join(' · ');
  } else if (name === 'exec_process') target = trace.command ?? text(args.command);
  else if (name === 'search_memory') target = text(args.query);

  let detail = label[1];
  if (name === 'mark_memory_relation' && trace.status === 'running') {
    detail = args.relation_kind === 'SUPERSEDES' ? '正在标记取代关系'
      : args.relation_kind === 'CONTRADICTS' ? '正在标记冲突关系' : detail;
  }
  if (trace.status === 'cancelled') detail = trace.resultState || trace.resultText !== undefined
    ? '操作已取消' : '未记录到操作结果';
  else if (trace.status === 'failed') detail = failure(trace, result);
  else if (trace.status === 'completed') {
    detail = label[2];
    const status = text(result.status).toLowerCase();
    switch (name) {
      case 'read_file': {
        const total = number(result.total_lines);
        if (total !== undefined) detail = `已读取文件 · 共 ${total} 行${result.truncated === true || (number(result.offset) ?? 1) > 1 ? '，本次仅返回部分内容' : ''}`;
        break;
      }
      case 'search_content':
      case 'find_files': {
        const total = number(result.total_count);
        if (total !== undefined) detail = `找到 ${total} ${name === 'find_files' ? '个文件' : result.output_mode === 'files_only' ? '个包含匹配的文件' : '行匹配'}${result.truncated === true || (number(args.offset) ?? 0) > 0 ? '，本次仅返回部分结果' : ''}`;
        break;
      }
      case 'write_file': {
        const bytes = number(result.bytes_written);
        if (bytes !== undefined) detail = `已写入 ${bytes} 字节`;
        break;
      }
      case 'read_artifact': {
        const chars = number(result.returned_chars);
        if (chars !== undefined) detail = `已读取 ${chars} 个字符${result.has_more === true ? '，还有后续内容' : '，已到末尾'}`;
        if (result.source_coverage === 'RETAINED_SNAPSHOT') detail += ' · 仅保留了部分原始输出';
        break;
      }
      case 'exec_process':
      case 'manage_process': {
        const process = result;
        const exitCode = number(process.exit_code);
        // The tool invocation can complete while the managed process keeps running.
        if (name === 'manage_process') {
          detail = ({ poll: '已查看命令状态', wait: '已等待命令',
            write: '已发送输入', submit: '已发送输入', close_stdin: '已结束输入',
          } as Record<string, string>)[action] ?? detail;
        }
        if (Array.isArray(result.processes)) detail = `本次列出 ${result.processes.length} 个命令进程`;
        else if (process.timed_out === true || process.status === 'timeout') detail = '命令已超时';
        else if (process.status === 'killed') detail = '命令已停止';
        else if (process.status === 'blocked') detail = '命令未获准执行';
        else if (process.status === 'running' || process.yielded_to_background === true) {
          detail = runningProcessDetail(name, action);
        }
        else if (process.status === 'success' && (name === 'exec_process' || action === 'poll' || action === 'wait')) detail = '命令执行完成';
        else if (exitCode === -1) detail = process.status === 'error' ? '命令执行失败' : '命令退出状态尚未确定';
        else if (exitCode !== undefined && exitCode !== 0) detail = `命令未成功，退出码 ${exitCode}`;
        else if (exitCode === 0 && (name === 'exec_process' || action === 'poll' || action === 'wait')) detail = '命令执行完成';
        break;
      }
      case 'query_processes':
        detail = Array.isArray(result.processes) ? `本次列出 ${result.processes.length} 个命令进程`
          : text(record(result.process).status) === 'running' ? '命令仍在运行' : detail;
        break;
      case 'exec_cell':
      case 'manage_cell':
        detail = ({ queued: '脚本等待执行', running: '脚本仍在运行', completed: '脚本执行完成',
          failed: '脚本执行失败', terminated: '脚本已停止' } as Record<string, string>)[text(result.state)] ?? detail;
        if (result.has_more === true) detail += ' · 还有后续输出';
        break;
      case 'query_cells':
        if (Array.isArray(result.cells)) detail = `本次列出 ${result.cells.length} 个脚本`;
        break;
      case 'query_monitors':
      case 'manage_monitor':
        detail = status === 'registered' ? '已开启进展通知'
          : status === 'cancelled' ? '已取消进展通知'
          : status === 'rejected' ? '未能更改进展通知'
          : Array.isArray(result.monitors) ? `本次列出 ${result.monitors.length} 项进展通知` : detail;
        break;
      case 'todo': {
        const counts = object(result.counts);
        if (status === 'cleared') detail = '已清空工作清单';
        else if (number(counts.total) !== undefined && number(counts.completed) !== undefined) {
          detail = `工作清单已更新 · ${counts.completed}/${counts.total} 项已完成`;
        }
        break;
      }
      case 'search_memory':
        if (Array.isArray(result.memories)) detail = `本次找到 ${result.memories.length} 条相关记忆`;
        break;
      case 'mark_memory_relation': {
        const relation = result.relation_kind === 'SUPERSEDES' ? '取代关系'
          : result.relation_kind === 'CONTRADICTS' ? '冲突关系' : '记忆关系';
        if (status === 'saved') detail = `已标记${relation}`;
        else if (status === 'already_present') detail = `${relation}已存在`;
        break;
      }
      case 'manage_capability':
        detail = ({ applied: '配置变更已应用', rejected: '配置变更未被接受', conflict: '配置已变化，本次变更未应用',
          cancelled: '已取消配置变更' } as Record<string, string>)[status] ?? detail;
        if (status === 'applied' && (result.adoption === 'PARTIAL' || object(result.adoption).status === 'PARTIAL')) {
          detail = '配置变更已保存，部分配置尚未生效';
        }
        break;
      case 'reload_capabilities':
        if (status === 'partial') detail = '部分配置未能刷新';
        break;
    }
  }
  return { title, subtitle: [detail, compact(target)].filter(Boolean).join(' · ') };
}

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
