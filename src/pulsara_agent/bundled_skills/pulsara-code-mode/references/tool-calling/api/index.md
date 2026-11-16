# Code Mode API reference

Read the relevant tool page before composing calls or accessing result fields. These pages describe the actual JS directory; they do not add native tools or expose Cell variables to ROOT. TypeScript here describes interfaces; write JavaScript in a Cell, without copying export, interface or import declarations.

Calls take one declared object or array argument. Await the complete saved business value; successful delivery of a SUCCESS value resolves normally. A thrown error may carry the actual complete business value as err.result. Its meta.status remains SUCCESS, ERROR or CANCELLED according to the business outcome; a save or delivery failure does not change a known SUCCESS. Read the error's message for the save or delivery failure. Errors without a business value omit it. Narrow each page's Value type by meta.status when handling an actual err.result. Common early rejection or execution failures contain only meta and text content; domain failures keep their declared fields. Do not repeat an action to recover its response.

Tool-call errors have name "ToolError", code and message, and result when an actual business value exists; ordinary JavaScript errors such as TypeError need not have this interface.

Missing fields differ from null, and content item order is preserved. JsonValue means JSON null, boolean, number, string, array or object, including unknown external MCP business values. Array length and numeric constraints remain those of the actual argument schema, including displayed annotations.

Use console.log or a top-level return to disclose selected values to ROOT. Values held only in Cell memory are not model-visible. Native observation time, page controls and NEXT are outside these JS values. Actual image items are recognized recursively in explicit output; bare Targets remain text.

## agents

- [tools.query_agents](query_agents.md)
- [tools.run_agent](run_agent.md)
- [tools.send_agent_message](send_agent_message.md)
- [tools.stop_agent](stop_agent.md)
- [tools.wait_agent](wait_agent.md)

## decision

- [tools.decide](decide.md)

## files

- [tools.edit_file](edit_file.md)
- [tools.find_files](find_files.md)
- [tools.read_file](read_file.md)
- [tools.search_file_content](search_file_content.md)
- [tools.write_file](write_file.md)

## history

- [tools.read_session_content](read_session_content.md)
- [tools.search_session_content](search_session_content.md)
- [tools.search_sessions](search_sessions.md)

## mcp

- [tools.call_mcp_tool](call_mcp_tool.md)
- [tools.query_capabilities](query_capabilities.md)
- [tools.read_mcp_resource](read_mcp_resource.md)

## media

- [tools.read_transcript](read_transcript.md)
- [tools.read_video_frames](read_video_frames.md)
- [tools.view_image](view_image.md)

## terminal

- [tools.exec_process](exec_process.md)
- [tools.manage_process](manage_process.md)
- [tools.query_processes](query_processes.md)
