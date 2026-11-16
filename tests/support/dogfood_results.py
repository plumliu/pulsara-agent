"""Current complete-result and Cell-output observations for controlled dogfood."""

from __future__ import annotations

import asyncio
from time import monotonic

from psycopg.rows import dict_row

from pulsara_agent.conversation_kernel.tool_outputs import read_accepted_tool_business_value
from pulsara_agent.ports.tool_execution import thaw_tool_json_object
from pulsara_agent.storage.postgres_connection_provider import PostgresConnectionLane


def _tool_trace(session) -> list[dict[str, object]]:
    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR, row_factory=dict_row,
        deadline_monotonic=monotonic()+30,
    ) as connection:
        rows = connection.execute(
            """SELECT r.*, request.entry_sequence AS request_sequence,
                      result.entry_sequence AS result_sequence,
                      b.tool_call_id AS trace_call_id,b.tool_name AS trace_tool_name,
                      b.tool_arguments AS trace_arguments,i.source_kind,
                      i.source_exec_attempt_id,i.call_sequence,b.ordinal AS call_ordinal
               FROM pulsara_v3.tool_results r
               JOIN pulsara_v3.tool_invocations i ON i.session_id=r.session_id AND i.id=r.invocation_id
               JOIN pulsara_v3.content_parts b ON b.session_id=i.session_id
                 AND b.entry_id=i.assistant_entry_id AND b.tool_call_id=i.tool_call_id AND b.type='tool_call'
               JOIN pulsara_v3.transcript_entries request ON request.session_id=b.session_id AND request.id=b.entry_id
               JOIN pulsara_v3.transcript_entries result ON result.session_id=r.session_id AND result.id=r.result_entry_id
               WHERE r.session_id=%s AND i.source_kind='MODEL_TOOL_CALL'
               UNION ALL
               SELECT r.*,request.entry_sequence AS request_sequence,
                      result.entry_sequence AS result_sequence,
                      i.id AS trace_call_id,i.tool_name AS trace_tool_name,
                      i.original_arguments AS trace_arguments,i.source_kind,
                      i.source_exec_attempt_id,i.call_sequence,b.ordinal AS call_ordinal
               FROM pulsara_v3.tool_results r
               JOIN pulsara_v3.tool_invocations i ON i.session_id=r.session_id AND i.id=r.invocation_id
               JOIN pulsara_v3.tool_execution_attempts source ON source.session_id=i.session_id AND source.id=i.source_exec_attempt_id
               JOIN pulsara_v3.content_parts b ON b.session_id=source.session_id
                 AND b.entry_id=source.assistant_entry_id AND b.tool_call_id=source.tool_call_id AND b.type='tool_call'
               JOIN pulsara_v3.transcript_entries request ON request.session_id=b.session_id AND request.id=b.entry_id
               JOIN pulsara_v3.transcript_entries result ON result.session_id=r.session_id AND result.id=r.result_entry_id
               WHERE r.session_id=%s AND i.source_kind='CODE_MODE_CALL'
               ORDER BY request_sequence,call_ordinal,call_sequence NULLS FIRST""",
            (session.session_id,session.session_id),
        ).fetchall()
        return [{
            "request_sequence": row["request_sequence"],
            "result_sequence": row["result_sequence"],
            "tool_call_id": row["trace_call_id"],
            "tool_name": row["trace_tool_name"],
            "source_kind": row["source_kind"],
            "source_exec_attempt_id": row["source_exec_attempt_id"],
            "call_sequence": row["call_sequence"],
            "arguments": row["trace_arguments"],
            "result_state": row["result_state"],
            "result": thaw_tool_json_object(read_accepted_tool_business_value(connection,row)),
        } for row in rows]


def _phase_cell_outputs(session, cell_ids):
    from pulsara_agent.conversation_kernel._repository.queue_content import read_input_owner

    with session.repository.connection_provider.connection(
        lane=PostgresConnectionLane.INSPECTOR,row_factory=dict_row,
        deadline_monotonic=monotonic()+30,
    ) as connection:
        rows=connection.execute("SELECT * FROM pulsara_v3.prompt_queue_items WHERE session_id=%s AND input_origin='CELL_OUTPUT' ORDER BY queue_sequence",(session.session_id,)).fetchall()
        outputs=[]
        for row in rows:
            if row["source_cell_id"] not in cell_ids:
                continue
            value=read_input_owner(connection,row=row,queue_item_id=str(row["id"]))
            outputs.append({"status":row["status"],"cell_id":value.cell_id,
                "source_turn_id":value.source_turn_id,"source_exec_attempt_id":value.source_exec_attempt_id,
                "record_sequence":value.record_sequence,"record":thaw_tool_json_object(value.record)})
        return outputs


async def _settle_code_mode_phase(session, *, trace_start):
    """Join actual admitted output within the caller's diagnostic deadline."""
    while True:
        if session._closing or session._closed:
            raise RuntimeError("Host closed before dogfood Cell output settled")
        if session._code_mode.snapshot_cell_index() or session._active_task is not None:
            await asyncio.sleep(0.01)
            continue
        trace=(await asyncio.to_thread(_tool_trace,session))[trace_start:]
        cell_ids={row["result"]["target"]["cell_id"] for row in trace
            if row["tool_name"]=="exec_cell" and row["result_state"]=="SUCCESS"}
        outputs=await asyncio.to_thread(_phase_cell_outputs,session,cell_ids)
        if session._closing or session._closed:
            raise RuntimeError("Host closed before dogfood Cell output settled")
        if session._code_mode.snapshot_cell_index() or session._active_task is not None:
            continue
        failures=[output for output in outputs if output["record"]["kind"]=="diagnostic" or output["status"] not in {"PENDING","CONSUMED"}]
        if failures:
            raise AssertionError(f"dogfood Cell output failed: {failures!r}")
        delivered={output["cell_id"] for output in outputs
            if output["status"]=="CONSUMED" and output["record"]["kind"] in {"log","return"}}
        pending=any(output["status"]=="PENDING" for output in outputs)
        if cell_ids<=delivered and not pending:
            return outputs
        if not pending:
            raise AssertionError(f"settled Cells did not publish their required output: {cell_ids-delivered!r}")
        await asyncio.sleep(0.01)


def _scrub(value: object, secrets: tuple[str,...]) -> object:
    if isinstance(value,str):
        for secret in secrets:
            if secret:
                value=value.replace(secret,"<PULSARA_API_KEY>")
        return value
    if isinstance(value,dict):
        return {str(_scrub(key,secrets)):_scrub(item,secrets) for key,item in value.items()}
    if isinstance(value,(tuple,list)):
        return [_scrub(item,secrets) for item in value]
    return value
