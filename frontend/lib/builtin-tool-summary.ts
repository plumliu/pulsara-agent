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
  exec_process: ['运行命令', '命令正在执行', '已执行命令'],
  query_processes: ['查看命令', '正在读取命令状态', '已读取命令状态'],
  manage_process: ['管理命令', '正在处理命令进程', '已查看命令状态'],
  exec_cell: ['运行脚本', '正在接纳脚本', '脚本已接纳'],
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
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function argumentsValue(value?: string): Record<string, unknown> {
  try { return object(JSON.parse(value ?? '')); } catch { return {}; }
}
const text = (value: unknown) => typeof value === 'string' ? value : '';
const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : undefined;
function compact(value: string): string {
  const line = value.replace(/\s+/g, ' ').trim();
  return line.length > 120 ? `${line.slice(0, 120)}…` : line;
}
const processActions: Record<string, string> = {
  poll: '查看命令状态', wait: '等待命令结束', write: '向命令输入内容',
  submit: '向命令提交输入', close_stdin: '结束命令输入', kill: '停止命令',
};
function processDetail(name: string, action: string, meta: Record<string, unknown>): string | undefined {
  if (meta.process_status === 'RUNNING') {
    if (name === 'exec_process') return '命令仍在后台运行';
    return ({poll:'命令仍在运行',wait:'等待结束，命令仍在运行',write:'已发送输入 · 命令仍在运行',
      submit:'已发送输入 · 命令仍在运行',close_stdin:'已结束输入 · 命令仍在运行',kill:'已请求停止，命令仍在运行'} as Record<string,string>)[action] ?? '命令仍在运行';
  }
  if (meta.process_status === 'EXITED') {
    const code=number(meta.exit_code);
    if (code !== undefined) return code === 0 ? '命令执行完成' : `命令未成功，退出码 ${code}`;
  }
  return undefined;
}
function cellDetail(meta: Record<string, unknown>): string | undefined {
  return ({queued:'脚本等待执行',running:'脚本仍在运行',completed:'脚本执行完成',
    failed:'脚本执行失败',terminated:'脚本已停止'} as Record<string,string>)[text(meta.cell_status)];
}
export function builtinToolSummary(trace: ToolTrace): {title:string;subtitle:string} | undefined {
  const name=trace.toolName ?? '';
  const label=labels[name];
  if (!label) return undefined;
  const args=argumentsValue(trace.argumentsJson), value=trace.businessValue;
  const meta=object(value?.meta), action=text(args.action), targetValue=object(value?.target);
  const content=value?.content ?? [];
  const title=(name === 'read_file' && object(args.target).kind === 'NEXT' ? '读取内容'
    : name === 'manage_process' ? processActions[action]
    : name === 'manage_cell' ? ({wait:'等待脚本结束',stop:'停止脚本'} as Record<string,string>)[action]
    : name === 'query_capabilities' && Object.hasOwn(args,'target') ? '查看能力详情' : undefined) ?? label[0];
  let target='';
  if (['read_file','view_image','edit_file','write_file','search_content','find_files'].includes(name)) {
    target=text(targetValue.path) || text(object(args.target).path);
    if (name === 'view_image' && (targetValue.kind === 'IMAGE_REF' || object(args.target).kind === 'IMAGE_REF')) target='对话中已保存的图片';
    if (name === 'search_content' || name === 'find_files') target=[text(name === 'find_files' ? args.glob : args.pattern),target].filter(Boolean).join(' · ');
  } else if (name === 'exec_process') target=trace.command ?? text(args.command);
  else if (name === 'search_memory') target=text(args.query);
  let detail=label[1];
  if (trace.status === 'cancelled') detail=trace.resultState || value ? '操作已取消' : '未记录到操作结果';
  else if (trace.status === 'failed') detail=(name === 'exec_process' || name === 'manage_process' ? processDetail(name,action,meta) : undefined) ?? '操作失败';
  else if (trace.status === 'completed') {
    detail=label[2];
    switch(name) {
      case 'read_file': {
        const total=number(meta.total_lines);
        if (total !== undefined) detail=`已读取文件 · 共 ${total} 行`;
        break;
      }
      case 'write_file': {
        const bytes=number(meta.bytes_written);
        if (bytes !== undefined) detail=`已写入 ${bytes} 字节`;
        break;
      }
      case 'find_files':
      case 'search_content': {
        const targets=content.filter(part=>part.type === 'target').length;
        const records=content.filter(part=>part.type === 'object').length;
        detail=args.output_mode === 'count' ? `本次列出 ${records} 个文件的匹配行数`
          : name === 'find_files' || args.output_mode === 'files_only' ? `本次找到 ${targets} 个文件` : `本次找到 ${records} 行匹配`;
        break;
      }
      case 'exec_process':
      case 'manage_process': detail=processDetail(name,action,meta) ?? detail; break;
      case 'query_processes': {
        const records=content.filter(part=>part.type === 'object');
        detail=Object.hasOwn(args,'target') && records.length === 1
          ? processDetail(name,'',object(records[0].value)) ?? detail
          : `本次列出 ${records.length} 个命令进程`;
        break;
      }
      case 'query_cells':
        detail=Object.hasOwn(args,'target') ? cellDetail(meta) ?? detail
          : `本次列出 ${content.filter(part=>part.type === 'object').length} 个脚本`;
        break;
      case 'manage_cell': detail=cellDetail(meta) ?? detail; break;
      case 'mark_memory_relation': detail=args.relation_kind === 'SUPERSEDES' ? '已标记取代关系' : args.relation_kind === 'CONTRADICTS' ? '已标记冲突关系' : '已标记记忆关系'; break;
      case 'search_memory': detail=`本次找到 ${content.filter(part=>part.type === 'object').length} 条相关记忆`; break;
      case 'open_in_pulsara': {
        const accepted=Array.isArray(trace.resultFacts?.presentation_targets) ? trace.resultFacts.presentation_targets.length : 0;
        detail=accepted ? `已请求展示 ${accepted} 个目标` : '展示请求已处理';
        break;
      }
    }
  }
  return {title,subtitle:[detail,compact(target)].filter(Boolean).join(' · ')};
}
