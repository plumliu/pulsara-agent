import type { ToolTrace } from './pulsara-types';

// Presentation only. Names and result fields follow builtin_catalog and each
// production tool owner; MCP direct and meta tools keep their existing views.
const labels: Record<string, [title: string, pending: string, completed: string]> = {
  read_file: ['读取文件', '正在读取文件', '已读取文件'],
  view_image: ['查看图片', '正在读取图片', '已读取图片'],
  search_files: ['搜索文件', '正在搜索', '搜索已结束'],
  edit_file: ['修改文件', '正在修改文件', '已修改文件'],
  write_file: ['写入文件', '正在写入文件', '已写入文件'],
  artifact_read: ['读取完整输出', '正在读取之前保留的输出', '已读取保留的输出'],
  terminal: ['运行命令', '命令正在执行', '已执行命令'],
  terminal_process: ['管理命令', '正在处理命令进程', '已查看命令状态'],
  terminal_monitor: ['关注命令进展', '正在设置进展通知', '已处理通知设置'],
  todo: ['更新工作清单', '正在更新工作清单', '已更新工作清单'],
  spawn_agent: ['创建子任务', '正在创建子任务', '已创建子任务'],
  create_agent_tasks: ['创建子任务', '正在创建子任务', '已创建子任务'],
  list_agents: ['查看子任务', '正在查看子任务状态', '已读取子任务状态'],
  wait_agent: ['等待子任务', '正在等待子任务进展', '已等待子任务'],
  stop_agent: ['停止子任务', '正在请求停止子任务', '已请求停止子任务'],
  send_agent_message: ['联系子任务', '正在发送补充信息', '已发送补充信息'],
  report_agent_result: ['提交子任务结果', '正在提交子任务结果', '已提交子任务结果'],
  enter_plan: ['开始规划', '正在进入规划模式', '已进入规划模式'],
  ask_plan_question: ['确认方案细节', '等待你的选择', '已记录你的选择'],
  exit_plan: ['提交方案', '正在提交方案', '方案已提交，等待你的确认'],
  memory_search: ['搜索记忆', '正在查找相关记忆', '记忆搜索已结束'],
  memory_get: ['读取记忆', '正在读取已保存的记忆', '已读取记忆'],
  memory_explain: ['查看记忆来源', '正在查看记忆的来源与审核记录', '已读取记忆的来源与审核记录'],
  remember: ['提交记忆', '正在提交记忆', '已提交记忆'],
  mark_memory_relation: ['标记记忆关系', '正在标记记忆关系', '已处理记忆关系'],
  manage_capability: ['管理扩展能力', '正在处理连接或插件配置', '已处理配置请求'],
  reload_capabilities: ['刷新扩展能力', '正在刷新技能、连接与自动操作', '已刷新技能、连接与自动操作'],
  reload_hooks: ['刷新自动操作', '正在刷新自动操作配置', '已刷新自动操作配置'],
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

const taskStates: Record<string, string> = {
  active: '正在执行', pending_start: '等待开始', waiting_dependency: '等待前置任务',
  completed: '已完成', cancelled: '已取消', failed: '执行失败',
  interrupted: '已中断', blocked_dependency_failed: '前置任务失败，暂时无法开始',
};
const processActions: Record<string, string> = {
  list: '查看命令列表', poll: '查看命令状态', wait: '等待命令结束',
  write: '向命令输入内容', submit: '向命令提交输入', close_stdin: '结束命令输入', kill: '停止命令',
};
const monitorActions: Record<string, string> = {
  register: '关注命令进展', list: '查看进展通知', cancel: '取消进展通知',
};

function runningProcessDetail(name: string, action: string): string {
  if (name === 'terminal') return '命令仍在后台运行';
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
  };
  const states: Record<string, string> = {
    PERMISSION_DENIED: '未获授权，未执行操作', INVALID_ARGUMENTS: '参数不正确，未执行操作',
    TOOL_UNAVAILABLE: '工具当前不可用', RESOURCE_EXHAUSTED: '资源不足，操作未完成',
    SYSTEM_ERROR: '本地服务出错，操作未完成',
  };
  if (trace.toolName === 'terminal' || trace.toolName === 'terminal_process') {
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
  if (trace.toolName === 'report_agent_result' && result.status === 'not_accepted') return '子任务结果未被接收';
  return errors[text(result.error)] ?? states[trace.resultState ?? ''] ?? '操作失败';
}

export function builtinToolSummary(trace: ToolTrace): { title: string; subtitle: string } | undefined {
  const name = trace.toolName ?? '';
  const label = labels[name];
  if (!label) return undefined;
  const args = parse(trace.argumentsJson);
  const result = parse(trace.resultText);
  const action = text(args.action);
  const title = (name === 'terminal_process' ? processActions[action]
    : name === 'terminal_monitor' ? monitorActions[action] : undefined) ?? label[0];
  let target = '';
  if (['read_file', 'view_image', 'edit_file', 'write_file', 'search_files'].includes(name)) {
    target = text(result.path) || text(args.path);
    if (name === 'view_image' && args.image_ref) target = '对话中已保存的图片';
    if (name === 'search_files') target = [text(args.pattern), target].filter(Boolean).join(' · ');
  } else if (name === 'terminal') target = trace.command ?? text(args.command);
  else if (name === 'spawn_agent') target = text(args.task_name);
  else if (name === 'memory_search') target = text(args.query);

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
      case 'search_files': {
        const total = number(result.total_count);
        if (total !== undefined) detail = `找到 ${total} ${Array.isArray(result.files) ? '个文件' : '处匹配'}${result.truncated === true ? '，本次仅返回部分结果' : ''}`;
        break;
      }
      case 'write_file': {
        const bytes = number(result.bytes_written);
        if (bytes !== undefined) detail = `已写入 ${bytes} 字节`;
        break;
      }
      case 'artifact_read': {
        const chars = number(result.returned_chars);
        if (chars !== undefined) detail = `已读取 ${chars} 个字符${result.has_more === true ? '，还有后续内容' : '，已到末尾'}`;
        if (result.source_coverage === 'RETAINED_SNAPSHOT') detail += ' · 仅保留了部分原始输出';
        break;
      }
      case 'terminal':
      case 'terminal_process': {
        const process = result;
        const exitCode = number(process.exit_code);
        // The tool invocation can complete while the managed process keeps running.
        if (name === 'terminal_process') {
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
        else if (process.status === 'success' && (name === 'terminal' || action === 'poll' || action === 'wait')) detail = '命令执行完成';
        else if (exitCode === -1) detail = process.status === 'error' ? '命令执行失败' : '命令退出状态尚未确定';
        else if (exitCode !== undefined && exitCode !== 0) detail = `命令未成功，退出码 ${exitCode}`;
        else if (exitCode === 0 && (name === 'terminal' || action === 'poll' || action === 'wait')) detail = '命令执行完成';
        break;
      }
      case 'terminal_monitor':
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
      case 'spawn_agent':
      case 'stop_agent':
        detail = taskStates[status] ? `子任务${taskStates[status]}` : detail;
        break;
      case 'create_agent_tasks':
      case 'list_agents': {
        const total = count(result.tasks);
        if (total !== undefined) detail = name === 'create_agent_tasks' ? `已创建 ${total} 个子任务` : `本次列出 ${total} 个子任务`;
        if (result.has_more === true) detail += '，还有后续任务';
        break;
      }
      case 'wait_agent':
        detail = ({ timeout: '已等待子任务', steer_available: '已收到你的补充',
          nothing_pending: '没有待处理的子任务', predicate_satisfied: '已有子任务结束' } as Record<string, string>)[text(result.outcome)] ?? detail;
        break;
      case 'send_agent_message':
        if (status === 'queued') detail = '已发送补充信息';
        break;
      case 'report_agent_result':
        if (status === 'accepted') detail = '子任务结果已接收';
        if (status === 'not_accepted') detail = '子任务结果未被接收';
        break;
      case 'memory_search':
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
      case 'reload_hooks':
        if (status === 'partial') detail = '部分配置未能刷新';
        break;
    }
  }
  return { title, subtitle: [detail, compact(target)].filter(Boolean).join(' · ') };
}
