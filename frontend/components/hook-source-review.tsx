const record = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const hookEvents: Record<string, string> = {SessionStart: '对话开始时', SessionEnd: '对话结束时', UserPromptSubmit: '发送消息时', PreToolUse: '工具调用前', PermissionRequest: '请求工具权限时', PostToolUse: '工具调用完成后', PreCompact: '压缩上下文前', PostCompact: '压缩上下文后', SubagentStart: '任务开始时', SubagentStop: '任务结束时', Stop: '模型准备结束时'};

export function HookSourceReview({hook}: {hook: Record<string, unknown>}) {
  return <>
      <div className="hook-review-source">
        <p className="hook-review-source-title">来源：{hook.plugin_id ? String(hook.plugin_id) : '本地 Hook'}</p>
        <pre className="capability-source-path" style={{whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}>{String(hook.path)}</pre>
      </div>
      {Array.isArray(hook.diagnostics) && hook.diagnostics.map((item, index) => <p className="capability-inline-error" key={`diagnostic-${index}`}>{String(record(item).message)}</p>)}
      {hook.tool_inventory_complete != null && <p>当前匹配清单是观察结果；后续满足原始规则的工具也可触发。</p>}
      {hook.tool_inventory_complete === false && <p>当前远端工具目录尚不完整，匹配清单仅包含已观察到的工具。</p>}
      {Array.isArray(hook.definitions) && hook.definitions.map((value, index) => {
        const definition = record(value);
        const operations = Array.isArray(definition.matched_operations) ? definition.matched_operations.map(String) : [];
        return <div className="hook-review-definition" key={index}>
          <p className="hook-review-event"><span className="hook-review-number">{index + 1}.</span>{hookEvents[String(definition.event)] ?? String(definition.event)}</p>
          {definition.is_tool_event === true && <p>适用操作：{operations.length ? operations.join('、') : '当前未发现匹配工具'}</p>}
          {definition.matches_all === true && <p>此规则也覆盖后续满足原规则的工具；当前清单不是授权白名单。</p>}
          <pre style={{whiteSpace: 'pre-wrap', overflowWrap: 'anywhere'}}>{String(definition.command)}</pre>
          <details className="hook-review-details"><summary>原始匹配规则与执行详情</summary>
            <div className="hook-review-details-content">
            <p>原始 matcher：<code>{String(definition.matcher)}</code></p>
            <p>脚本收到 Pulsara 原生工具名与参数；匹配别名不转换脚本输入。</p>
            {Array.isArray(definition.matching_aliases) && definition.matching_aliases.map((item, n) => {const mapping = record(item); return <p key={n}>{Array.isArray(mapping.aliases) ? mapping.aliases.join('、') : ''} → {String(mapping.tool_name)}</p>;})}
            <p>声明环境：<code>{JSON.stringify(hook.declaration_environment)}</code></p>
            {definition.commandWindows != null && <><p>Windows 命令</p><pre>{String(definition.commandWindows)}</pre></>}
            <p>超时：{String(definition.timeout)} 秒</p><p>异步：{String(definition.async)}</p><p>上下文阈值：{String(definition.additionalContextLimit)}</p>
            <p>{String(definition.statusMessage ?? '')}</p>
            </div>
          </details>
        </div>;
      })}
  </>;
}
