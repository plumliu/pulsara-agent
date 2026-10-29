"""HostSession-owned, process-local cells and bounded Node sidecar transport.

The owner holds no business scheduler or Work/result database. Every guest tool
submission calls the kernel's shared executor with the admitted exec snapshot.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict, dataclass, field, replace
import json
import math
from pathlib import Path
from typing import Awaitable, Callable, Mapping
from types import MappingProxyType

from uuid import uuid4
from pulsara_agent.identity import random_id, require_id

from .contracts import (
    CellState, CodeModeError, CodeModeInvocation, CodeModeInvoke, CodeModeLimits,
    CodeModeObservation, CodeModePrepareBatch, CodeModeToolResult, CodeModeCellSnapshot,
)
from .targets import cell_target

_TERMINAL = frozenset({"completed", "failed", "terminated"})
_SAFE_INTEGER = 9_007_199_254_740_991
ArchiveOutput = Callable[[Mapping[str, object]], Awaitable[Mapping[str, object]]]


def _json(value: object) -> str:
    """Typed JSON bridge, never guess JSON from a TEXT business value."""
    def check(item: object) -> None:
        if item is None or type(item) in (str, bool):
            return
        if type(item) is int:
            if abs(item) > _SAFE_INTEGER:
                raise CodeModeError("unsafe_integer", "Bridge JSON integer exceeds JavaScript's safe range")
            return
        if type(item) is float:
            if not math.isfinite(item) or (item.is_integer() and abs(item) > _SAFE_INTEGER):
                raise CodeModeError("unsafe_number", "Bridge requires finite, safely representable JSON numbers")
            return
        if type(item) is list:
            for child in item:
                check(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child)
            return
        raise CodeModeError("invalid_json", "Bridge value is not a strict JSON value")
    try:
        check(value)
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        try:
            encoded.encode("utf-8")
        except UnicodeEncodeError:
            # JSON escapes preserve ECMAScript lone surrogates without inventing
            # replacement characters or putting invalid UTF-8 into canonical I/O.
            return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
        return encoded
    except (RecursionError, ValueError) as error:
        raise CodeModeError("invalid_json", "Bridge value is cyclic or cannot be encoded as JSON") from error


def _bytes(value: object) -> int:
    return len(_json(value).encode("utf-8")) + 1


@dataclass(slots=True)
class _Output:
    value: Mapping[str, object] | None
    charge: int = 0
    result: CodeModeToolResult | None = None
    sequence: int | None = None
    tool_name: str | None = None


@dataclass(slots=True)
class _Pending:
    request_id: str
    sequence: int | None
    tool_name: str | None
    charge: int
    task: asyncio.Task[None] | None = None
    delivered: bool = False
    handshake: asyncio.Future[None] | None = None


@dataclass(slots=True)
class _Cell:
    cell_id: str
    turn_id: str
    source_exec_attempt_id: str
    snapshot: object
    tool_catalog: Mapping[str, object]
    invoke: CodeModeInvoke
    prepare_batch: CodeModePrepareBatch
    admitted: asyncio.Future[None]
    on_cell_closed: Callable[[str], None] | None = None
    state: CellState = "queued"
    error: Mapping[str, object] | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    vm_released: asyncio.Event = field(default_factory=asyncio.Event)
    delivery_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    output: deque[_Output] = field(default_factory=deque)
    pending: dict[str, _Pending] = field(default_factory=dict)
    preparations: dict[str, Mapping[str, object]] = field(default_factory=dict)
    agent_message_cursors: dict[str, int] = field(default_factory=dict)
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event)
    reservations: dict[str, asyncio.Future[bool]] = field(default_factory=dict)
    observation: CodeModeObservation | None = None
    observed_count: int = 0
    terminal_delivered: bool = False

    @property
    def pending_calls(self) -> int:
        return sum(item.sequence is not None for item in self.pending.values())


class CodeModeHost:
    def __init__(self, *, limits: CodeModeLimits | None = None,
                 node_executable: Path | None = None) -> None:
        self.limits = limits or CodeModeLimits()
        package = Path(__file__).resolve().parent
        self._script = package / "node/sidecar.mjs"
        self._node = node_executable or package.parent / "_vendor/code_mode/node"
        self._cells: dict[str, _Cell] = {}
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr_reader: asyncio.Task[None] | None = None
        self._startup = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._configured: asyncio.Future[None] | None = None
        self._closed = False
        self._sidecar_failure: str | None = None

    async def _ensure_started(self) -> None:
        async with self._startup:
            if self._closed:
                raise CodeModeError("unavailable", "Code Mode Host is closed")
            if self._sidecar_failure is not None:
                raise CodeModeError("unavailable", self._sidecar_failure)
            if self._process is not None:
                if self._process.returncode is not None:
                    raise CodeModeError("unavailable", "Code Mode sidecar has exited; old cells cannot resume")
                return
            if not self._node.is_file():
                raise CodeModeError("runtime_unavailable", "Prepared Code Mode Node runtime is missing; run tools/prepare_code_mode.py before build/install")
            self._configured = asyncio.get_running_loop().create_future()
            self._process = await asyncio.create_subprocess_exec(
                str(self._node), str(self._script), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=self.limits.cell_bytes + self.limits.control_bytes,
            )
            self._reader = asyncio.create_task(self._read_messages(), name="code-mode-sidecar")
            self._stderr_reader = asyncio.create_task(self._drain_stderr(), name="code-mode-stderr")
            await self._send({"type": "configure", "limits": asdict(self.limits)})
            await self._configured

    async def _send(self, message: Mapping[str, object]) -> None:
        payload = _json(dict(message)).encode("utf-8") + b"\n"
        if len(payload) > self.limits.cell_bytes + self.limits.control_bytes:
            raise CodeModeError("ipc_boundary", "Code Mode IPC frame exceeds its physical boundary")
        async with self._write_lock:
            process = self._process
            if process is None or process.returncode is not None or process.stdin is None:
                raise CodeModeError("unavailable", "Code Mode sidecar is unavailable")
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as error:
                raise CodeModeError("unavailable", "Code Mode sidecar disconnected") from error

    async def _drain_stderr(self) -> None:
        assert self._process is not None and self._process.stderr is not None
        # Bounded diagnostic tail; guest console only uses the formal output path.
        tail = bytearray()
        while chunk := await self._process.stderr.read(min(4096, self.limits.control_bytes - self.limits.page_bytes)):
            tail.extend(chunk)
            if len(tail) > min(4096, self.limits.control_bytes - self.limits.page_bytes):
                del tail[:-min(4096, self.limits.control_bytes - self.limits.page_bytes)]
        if tail:
            diagnostic = tail.decode("utf-8", errors="replace")
            for cell in self._cells.values():
                if cell.state not in _TERMINAL:
                    cell.error = {"name": "SidecarError", "message": diagnostic}

    async def _read_messages(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        failure = "Code Mode sidecar exited; VM state cannot be resumed"
        try:
            while line := await self._process.stdout.readline():
                message = json.loads(line)
                if message["type"] == "configured":
                    assert self._configured is not None
                    self._configured.set_result(None)
                    continue
                cell = self._cells.get(message.get("cell_id", ""))
                if cell is None:
                    continue
                kind = message["type"]
                if kind == "admitted":
                    cell.admitted.set_result(None)
                elif kind == "rejected":
                    cell.admitted.set_exception(CodeModeError(message["code"], message["message"]))
                elif kind == "running":
                    if cell.state not in _TERMINAL:
                        cell.state = "running"
                elif kind == "output":
                    cell.output.append(_Output(message["record"], message["charge"]))
                elif kind in ("call", "prepare"):
                    self._accept_request(cell, message)
                elif kind == "retained":
                    reservation = cell.reservations.pop(message["reservation"], None)
                    if reservation is not None and not reservation.done():
                        reservation.set_result(message["accepted"])
                elif kind == "discard_preparation":
                    cell.preparations.pop(message["preparation"], None)
                elif kind == "delivered":
                    pending = cell.pending.get(str(message["request_id"]))
                    if pending is not None:
                        pending.delivered = True
                        pending.charge = 0  # Worker released the original parameters.
                        if pending.handshake is not None and not pending.handshake.done():
                            pending.handshake.set_result(None)
                elif kind == "terminal":
                    cell.state = message["state"]
                    cell.error = message.get("error")
                    self._notify_closed(cell)
                    cell.terminal_event.set()
                    cell.cancelled.set()
                    cell.preparations.clear()
                    for pending in cell.pending.values():
                        if pending.handshake is not None and not pending.handshake.done():
                            pending.handshake.set_exception(CodeModeError("unavailable", "Cell stopped before Promise delivery"))
                elif kind == "vm_released":
                    cell.vm_released.set()
                    asyncio.create_task(self._collect(cell))
                cell.changed.set()
        except (ValueError, KeyError, asyncio.LimitOverrunError) as error:
            failure = f"Code Mode sidecar protocol failed: {error}"
        finally:
            self._sidecar_failure = failure
            if self._process.returncode is None and not self._closed:
                try:
                    self._process.terminate()
                except ProcessLookupError:
                    pass
            if self._configured is not None and not self._configured.done():
                self._configured.set_exception(CodeModeError("runtime_unavailable", failure))
            for cell in tuple(self._cells.values()):
                if not cell.admitted.done():
                    cell.admitted.set_exception(CodeModeError("runtime_unavailable", failure))
                if cell.state not in _TERMINAL:
                    cell.state = "failed"
                    cell.error = {"name": "SidecarError", "message": failure}
                for pending in cell.pending.values():
                    if pending.handshake is not None and not pending.handshake.done():
                        pending.handshake.set_exception(CodeModeError("unavailable", failure))
                cell.vm_released.set()
                self._notify_closed(cell)
                cell.terminal_event.set()
                cell.cancelled.set()
                for reservation in cell.reservations.values():
                    if not reservation.done():
                        reservation.set_result(False)
                cell.reservations.clear()
                cell.changed.set()

    @staticmethod
    def _notify_closed(cell: _Cell) -> None:
        if cell.terminal_event.is_set() or cell.on_cell_closed is None:
            return
        try:
            cell.on_cell_closed(cell.cell_id)
        except Exception as error:
            cell.error = {**(cell.error or {}), "cleanup_error": str(error)[:512]}

    async def start(self, *, source: str, source_exec_attempt_id: str, turn_id: str,
                    snapshot: object, tool_catalog: Mapping[str, object], invoke: CodeModeInvoke,
                    prepare_batch: CodeModePrepareBatch | None = None,
                    on_cell_closed: Callable[[str], None] | None = None) -> str:
        if type(source) is not str or len(source.encode("utf-8")) > self.limits.value_bytes:
            raise CodeModeError("source_boundary", "Code Mode source exceeds the single-source UTF-8 boundary")
        if prepare_batch is None:
            from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import prepare_batch_refs
            prepare_batch = prepare_batch_refs
        # Freeze the complete exact schemas; dispatch still checks live capability facts.
        catalog_json = json.dumps(dict(tool_catalog), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        catalog = json.loads(catalog_json)
        await self._ensure_started()
        cell_id = random_id("cell")
        cell = _Cell(cell_id, turn_id, source_exec_attempt_id, snapshot, catalog, invoke,
                     prepare_batch, asyncio.get_running_loop().create_future(),
                     on_cell_closed=on_cell_closed)
        command = {"type": "start", "cell_id": cell_id, "source": source,
                   "tool_names": list(catalog), "catalog_bytes": len(catalog_json.encode("utf-8")) + 256}
        if _bytes(command) > self.limits.cell_bytes + self.limits.control_bytes:
            raise CodeModeError("ipc_boundary", "Code Mode source frame exceeds the current data boundary")
        self._cells[cell_id] = cell
        try:
            await self._send(command)
            await asyncio.shield(cell.admitted)
        except BaseException:
            if cell.admitted.done() and cell.admitted.exception() is not None:
                self._cells.pop(cell_id, None)
            else:
                await self.stop(cell_id)
            raise
        return cell_id

    def _accept_request(self, cell: _Cell, message: dict[str, object]) -> None:
        # No await between the live-cell decision and recording accepted submission.
        # stop() changes state in this same event-loop owner before sending control.
        if cell.cancelled.is_set():
            asyncio.create_task(self._safe_send({"type": "release", "cell_id": cell.cell_id,
                                                 "bytes": int(message["charge"])}))
            return
        request_id = str(message["request_id"])
        pending = _Pending(request_id,
                           int(message["sequence"]) if message["type"] == "call" else None,
                           str(message["tool_name"]) if message["type"] == "call" else None,
                           int(message["charge"]))
        cell.pending[request_id] = pending
        pending.task = asyncio.create_task(self._execute_request(cell, pending, message),
                                           name=f"code-mode:{cell.cell_id}:{request_id}")

    async def _execute_request(self, cell: _Cell, pending: _Pending,
                               message: Mapping[str, object]) -> None:
        result: CodeModeToolResult | None = None
        try:
            arguments = json.loads(str(message["arguments_json"]))
            if type(arguments) is not dict:
                raise CodeModeError("invalid_arguments", "Tool arguments must be a JSON object")
            _json(arguments)
            if cell.cancelled.is_set() or cell.state in _TERMINAL:
                raise CodeModeError("not_dispatched", "Cell stopped before tool submission")
            if pending.sequence is None:
                peers = cell.prepare_batch(arguments)
                # Keep only temporary identities, not an admitted batch or Work state.
                cell.preparations[pending.request_id] = peers
                result = CodeModeToolResult(value={"id": pending.request_id, "peers": peers})
            else:
                name = pending.tool_name
                assert name is not None
                if name not in cell.tool_catalog and name not in ("run_agent.batch", "wait_agent.works"):
                    raise CodeModeError("tool_unavailable", "Tool is not in this cell's frozen catalog")
                if name == "run_agent.batch":
                    from pulsara_agent.conversation_kernel.subagents.collaboration_contracts import validate_batch_identities
                    prepared = cell.preparations.pop(str(message.get("preparation")), None)
                    if prepared is None:
                        raise CodeModeError("invalid_batch", "Batch does not own prepared identities")
                    validate_batch_identities(arguments, prepared)
                invocation = CodeModeInvocation(cell.cell_id, pending.sequence, name, arguments,
                                                cell.snapshot, cell.source_exec_attempt_id,
                                                cell.agent_message_cursors, cell.cancelled)
                result = await cell.invoke(invocation)
            async with cell.delivery_lock:
                if cell.state not in _TERMINAL:
                    try:
                        await self._deliver(cell, pending, result)
                    except CodeModeError as error:
                        if cell.state not in _TERMINAL:
                            # Preserve accepted business effects, reject only the transport.
                            transport = CodeModeToolResult(state=result.state, error_code=error.code,
                                error_message=str(error), artifact=result.artifact,
                                value={"result_id": result.result_id, "result_entry_id": result.result_entry_id,
                                       "coverage": "PREVIEW", "reason": error.code})
                            await self._deliver(cell, pending, transport)
                if not pending.delivered and pending.sequence is not None:
                    await self._late_result(cell, pending, result)
        except Exception as error:
            code = error.code if isinstance(error, CodeModeError) else "bridge_error"
            failure = CodeModeToolResult(state="FAILED", error_code=code, error_message=str(error))
            async with cell.delivery_lock:
                if cell.state not in _TERMINAL:
                    try:
                        await self._deliver(cell, pending, failure)
                    except (CodeModeError, OSError):
                        pass
                if not pending.delivered and pending.sequence is not None:
                    await self._late_result(cell, pending, result or failure)
        finally:
            if pending.charge:
                await self._safe_send({"type": "release", "cell_id": cell.cell_id, "bytes": pending.charge})
            cell.pending.pop(pending.request_id, None)
            cell.changed.set()
            await self._collect(cell)

    async def _deliver(self, cell: _Cell, pending: _Pending, result: CodeModeToolResult) -> None:
        if result.error_code is None:
            value = result.value
        else:
            value = {"code": result.error_code, "message": result.error_message or "Tool failed",
                     "state": result.state, "result": result.value,
                     "artifact": dict(result.artifact) if result.artifact else None,
                     "result_id": result.result_id, "result_entry_id": result.result_entry_id}
        encoded = _json(value)
        if len(encoded.encode("utf-8")) > self.limits.value_bytes:
            raise CodeModeError("value_boundary", "Accepted tool result exceeds the bridge value boundary; use its original result or artifact")
        pending.handshake = asyncio.get_running_loop().create_future()
        await self._send({"type": "result", "cell_id": cell.cell_id,
                          "request_id": pending.request_id, "json": encoded,
                          "error": result.error_code is not None})
        await pending.handshake
        try:
            if result.delivery_succeeded is not None:
                await result.delivery_succeeded()
        except Exception as error:
            diagnostic = {"name": "DeliveryOwnerError", "code": "delivery_owner_error",
                          "message": str(error)[:512], "result_id": result.result_id,
                          "result_entry_id": result.result_entry_id}
            await self._safe_send({"type": "fail", "cell_id": cell.cell_id, "error": diagnostic})
            await cell.terminal_event.wait()
            raise CodeModeError("delivery_owner_error", "Tool value arrived, but its delivery owner failed") from error
        else:
            await self._safe_send({"type": "resume", "cell_id": cell.cell_id,
                                   "request_id": pending.request_id,
                                   "keep_preparation": pending.sequence is None})

    async def _retain_data(self, cell: _Cell, size: int) -> bool:
        if size <= 0:
            return True
        if self._process is None or self._process.returncode is not None or self._sidecar_failure is not None:
            return False
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        cell.reservations[key] = future
        try:
            await self._send({"type": "retain", "cell_id": cell.cell_id,
                              "reservation": key, "bytes": size})
            return await future
        except (CodeModeError, OSError):
            return False
        finally:
            cell.reservations.pop(key, None)

    async def _late_result(self, cell: _Cell, pending: _Pending, result: CodeModeToolResult) -> None:
        # Preserve canonical identity and lazily read its accepted projection.
        retained = replace(result, delivery_succeeded=None, error_message=None)
        if result.read_undelivered is not None:
            retained = replace(retained, value=None)
        pointer = {"sequence": pending.sequence, "tool_name": pending.tool_name,
                   "state": result.state, "result_id": result.result_id,
                   "result_entry_id": result.result_entry_id,
                   "artifact": dict(result.artifact) if result.artifact else None}
        size = _bytes(pointer) + 256
        if result.read_undelivered is None:
            try:
                size += _bytes(result.value)
            except CodeModeError:
                retained = replace(retained, value=None)
        charge = pending.charge
        if size > charge and not await self._retain_data(cell, size - charge):
            # Pending-call admission already reserved its small canonical locator.
            # Never retain the raw value when additional data cannot be admitted.
            retained = replace(retained, value=None, artifact=None)
            cell.error = {"name": "ResourceError", "code": "data_boundary",
                          "message": "Accepted late result exceeds the cell data boundary",
                          "result_id": result.result_id, "result_entry_id": result.result_entry_id}
        else:
            charge = max(charge, size)
        pending.charge = 0
        cell.output.append(_Output(None, charge=charge, result=retained,
                                   sequence=pending.sequence, tool_name=pending.tool_name))

    async def _safe_send(self, message: Mapping[str, object]) -> None:
        try:
            await self._send(message)
        except (CodeModeError, OSError):
            pass

    def _cell(self, cell_id: str) -> _Cell:
        try:
            require_id(cell_id, "cell")
        except ValueError as error:
            raise CodeModeError("invalid_arguments", str(error)) from error
        cell = self._cells.get(cell_id)
        if cell is None:
            raise CodeModeError("unavailable", "Code Mode cell is unavailable in this Host")
        return cell

    def capture_cell(self, cell_id: str) -> CodeModeCellSnapshot:
        """Capture source provenance and metadata without an output reservation."""
        self._require_query_available()
        cell = self._cell(cell_id)
        return CodeModeCellSnapshot(cell.cell_id, cell.state, bool(cell.output),
            cell.pending_calls, MappingProxyType(dict(cell.error)) if cell.error is not None else None,
            cell.source_exec_attempt_id)

    def _require_query_available(self) -> None:
        if (self._closed or self._sidecar_failure is not None
                or (self._process is not None and self._process.returncode is not None)):
            raise CodeModeError("unavailable", "Code Mode Host is unavailable")

    def query_cells(self, *, cursor: str | None = None, max_items: int = 50,
                    maximum_page_bytes: int | None = None) -> dict[str, object]:
        """Weakly consistent sorted inventory; cursors need not name a live Cell."""
        self._require_query_available()
        if cursor is not None:
            try:
                require_id(cursor, "cell")
            except ValueError as error:
                raise CodeModeError("invalid_arguments", "cursor must be a canonical Cell ID") from error
        if type(max_items) is not int or max_items < 1:
            raise CodeModeError("invalid_arguments", "max_items must be a positive integer")
        page_bytes = self.limits.page_bytes if maximum_page_bytes is None else maximum_page_bytes
        if type(page_bytes) is not int or not 0 < page_bytes <= self.limits.page_bytes:
            raise CodeModeError("invalid_arguments", "Cell query page must fit the existing model result boundary")
        # No history snapshot or continuation authority is retained. The tuple
        # contains only this event-loop observation's small public fields.
        rows = tuple({"target": cell_target(cell.cell_id), "state": cell.state,
                      "has_unread_output": bool(cell.output), "pending_calls": cell.pending_calls}
                     for cell in sorted(self._cells.values(), key=lambda item: item.cell_id)
                     if cursor is None or cell.cell_id > cursor)
        selected: list[dict[str, object]] = []
        response: dict[str, object] = {"cells": selected, "has_more": False, "next_cursor": None}
        for index, row in enumerate(rows):
            if len(selected) >= max_items:
                break
            has_more = index + 1 < len(rows)
            trial = {"cells": [*selected, row], "has_more": has_more,
                     "next_cursor": row["target"]["cell_id"] if has_more else None}
            if len(_json(trial).encode("utf-8")) > page_bytes:
                if not selected:
                    raise CodeModeError("output_resource_error", "A Cell inventory row exceeds the model result boundary")
                break
            selected.append(row)
            response = {"cells": selected, "has_more": has_more,
                        "next_cursor": row["target"]["cell_id"] if has_more else None}
        if len(_json(response).encode("utf-8")) > page_bytes:
            raise CodeModeError("output_resource_error", "Cell inventory envelope exceeds the model result boundary")
        return response

    async def observe(self, cell_id: str, yield_time_ms: int = 1_000, *,
                      archive_output: ArchiveOutput | None = None,
                      maximum_page_bytes: int | None = None) -> CodeModeObservation:
        if self._closed:
            raise CodeModeError("unavailable", "Code Mode Host is closed")
        if type(yield_time_ms) is not int or not 0 <= yield_time_ms <= 30_000:
            raise CodeModeError("invalid_arguments", "yield_time_ms must be between 0 and 30000")
        page_bytes = self.limits.page_bytes if maximum_page_bytes is None else maximum_page_bytes
        if type(page_bytes) is not int or page_bytes <= 0 or page_bytes > self.limits.page_bytes:
            raise CodeModeError("invalid_arguments", "Observation page must fit the existing model result boundary")
        cell = self._cell(cell_id)
        if cell.observation is not None:
            raise CodeModeError("busy", "Another reader owns this cell's output observation")
        # Reserve output consumption before waiting; stop() never needs this owner.
        reservation = CodeModeObservation(cell_id, cell.state, (), False, cell.pending_calls, cell.error)
        cell.observation = reservation
        try:
            if not cell.output and (cell.state not in _TERMINAL or cell.pending) and yield_time_ms:
                cell.changed.clear()
                try:
                    await asyncio.wait_for(cell.changed.wait(), yield_time_ms / 1000)
                except TimeoutError:
                    pass
            records: list[Mapping[str, object]] = []
            selected = 0
            archived = False
            for item in cell.output:
                candidate = item.value
                if candidate is None:
                    result = item.result
                    assert result is not None
                    body = (await result.read_undelivered() if result.read_undelivered else
                            {"result": result.value, "artifact": dict(result.artifact) if result.artifact else None,
                             "result_id": result.result_id, "result_entry_id": result.result_entry_id})
                    candidate = {"kind": "tool_result", "sequence": item.sequence,
                                 "tool_name": item.tool_name, "state": result.state, **body}
                trial = self._public_page(cell, [*records, candidate], False)
                if len(_json(trial).encode("utf-8")) > page_bytes:
                    if records:
                        break
                    if archive_output is None:
                        raise CodeModeError("output_resource_error", "Output record requires the existing artifact owner before it can fit a model result page")
                    candidate = dict(await archive_output(candidate))
                    archived = True
                    if len(_json(self._public_page(cell, [candidate], False)).encode("utf-8")) > page_bytes:
                        raise CodeModeError("output_resource_error", "Archived output reference exceeds the model result boundary")
                # The one-page observation is bounded by its pre-admitted control
                # scratch. Keep original records/accepted-result readers until ACK;
                # an abandoned artifact must be prepared for the next outer result.
                records.append(candidate)
                selected += 1
                if archived:
                    break
            observation = CodeModeObservation(cell_id, cell.state, tuple(records),
                selected < len(cell.output), cell.pending_calls, cell.error)
            if len(_json(observation.public_value()).encode("utf-8")) > page_bytes:
                raise CodeModeError("output_resource_error", "Cell output envelope exceeds the model result boundary")
            cell.observation = observation
            cell.observed_count = selected
            return observation
        except BaseException:
            if cell.observation is reservation:
                cell.observation = None
            raise

    @staticmethod
    def _public_page(cell: _Cell, records: list[Mapping[str, object]], has_more: bool) -> dict[str, object]:
        return CodeModeObservation(cell.cell_id, cell.state, tuple(records),
            has_more, cell.pending_calls, cell.error).public_value()

    async def acknowledge(self, observation: CodeModeObservation) -> None:
        cell = self._cell(observation.cell_id)
        if cell.observation is not observation:
            raise CodeModeError("invalid_observation", "Observation is not the current output owner")
        if observation.state in _TERMINAL:
            cell.terminal_delivered = True
        charge = 0
        for _ in range(cell.observed_count):
            charge += cell.output.popleft().charge
        cell.observed_count = 0
        cell.observation = None
        if charge:
            await self._safe_send({"type": "release", "cell_id": cell.cell_id, "bytes": charge})
        await self._collect(cell)

    def abandon(self, observation: CodeModeObservation) -> None:
        cell = self._cell(observation.cell_id)
        if cell.observation is not observation:
            raise CodeModeError("invalid_observation", "Observation is not the current output owner")
        cell.observation = None
        cell.observed_count = 0
        if self._closed:
            self._cells.pop(cell.cell_id, None)

    async def _collect(self, cell: _Cell) -> None:
        if self._closed and cell.observation is None and not cell.pending:
            self._cells.pop(cell.cell_id, None)
            return
        if (cell.terminal_delivered and cell.state in _TERMINAL and not cell.pending and not cell.output
                and cell.observation is None and cell.vm_released.is_set()):
            self._cells.pop(cell.cell_id, None)
            await self._safe_send({"type": "forget", "cell_id": cell.cell_id})

    async def _stop_cell(self, cell: _Cell) -> dict[str, object]:
        if cell.state not in _TERMINAL:
            cell.cancelled.set()
            await self._safe_send({"type": "stop", "cell_id": cell.cell_id})
            await cell.terminal_event.wait()
        return {"target": cell_target(cell.cell_id), "state": cell.state,
                "has_more": bool(cell.output), "pending_calls": cell.pending_calls}

    async def stop(self, cell_id: str) -> dict[str, object]:
        if self._closed:
            raise CodeModeError("unavailable", "Code Mode Host is closed")
        return await self._stop_cell(self._cell(cell_id))

    async def stop_turn(self, turn_id: str) -> None:
        for cell in tuple(self._cells.values()):
            if cell.turn_id == turn_id:
                await self._stop_cell(cell)

    def snapshot_cell_index(self) -> tuple[dict[str, object], ...]:
        if self._closed:
            return ()
        return tuple({"cell_id": cell.cell_id, "state": cell.state,
                      "has_unread_output": bool(cell.output), "pending_calls": cell.pending_calls}
                     for cell in self._cells.values())

    def freeze_compaction_handoff(self) -> tuple[dict[str, object], ...]:
        """Observe pending work on the owner loop without consuming or retaining it."""
        self._require_query_available()
        return tuple(
            {"target": cell_target(cell.cell_id), "state": cell.state,
             "has_unread_output": bool(cell.output), "pending_calls": cell.pending_calls}
            for cell in sorted(self._cells.values(), key=lambda item: item.cell_id)
            if (cell.state not in _TERMINAL or not cell.terminal_delivered
                or cell.output or cell.pending)
        )

    def has_live_cells(self) -> bool:
        return any(cell.state not in _TERMINAL or cell.pending for cell in self._cells.values())

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for cell in tuple(self._cells.values()):
            await self._stop_cell(cell)
        if self._process is not None:
            await self._safe_send({"type": "close"})
        # In-flight effects settle in their existing owner; VM termination never drops them.
        tasks = [pending.task for cell in self._cells.values() for pending in cell.pending.values()
                 if pending.task is not None]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._process is not None:
            if self._process.stdin is not None:
                self._process.stdin.close()
            await self._process.wait()
        if self._reader is not None:
            await self._reader
        if self._stderr_reader is not None:
            await self._stderr_reader
        # Existing outer canonical settlements may still ACK/abandon their
        # reserved page. A closed Host accepts no new handle observations.
        self._cells = {key: cell for key, cell in self._cells.items() if cell.observation is not None}
